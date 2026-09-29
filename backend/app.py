"""
Rubicon Flask application factory.
Server-side pagination & memory optimization build.

Run:
    cp .env.example .env         # fill in DATABASE_URL etc.
    pip install -r requirements.txt
    flask --app app init-db      # create tables
    flask --app app seed         # demo event/admin/roster
    flask --app app run -p 5001  # dev server  (or: gunicorn "app:app")
"""
import os

import click
from flask import Flask, current_app, jsonify, send_from_directory

from config import Config
from extensions import cors, db, limiter, migrate, oauth


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    migrate.init_app(app, db)
    cors.init_app(app, resources={r"/api/*": {"origins": Config.CORS_ORIGINS}},
                  supports_credentials=True)
    oauth.init_app(app)
    limiter.init_app(app)

    # Google OpenID Connect (identity sign-in). Photos scopes are added separately.
    oauth.register(
        name="google",
        client_id=Config.GOOGLE_CLIENT_ID,
        client_secret=Config.GOOGLE_CLIENT_SECRET,
        server_metadata_url=Config.GOOGLE_DISCOVERY_URL,
        client_kwargs={"scope": "openid email profile"},
    )

    # Google Drive per-event storage (separate client: offline access + drive.file
    # scope) so an event admin can connect/switch that event's own Drive account.
    oauth.register(
        name="google_drive",
        client_id=Config.GOOGLE_CLIENT_ID,
        client_secret=Config.GOOGLE_CLIENT_SECRET,
        server_metadata_url=Config.GOOGLE_DISCOVERY_URL,
        client_kwargs={"scope": Config.GOOGLE_DRIVE_SCOPE},
    )

    # Import models so migrations/create_all see them.
    from models import (  # noqa: F401
        Event, FaceDetection, IngestionJob, Participant, Photo,
        Photographer, StorageAccount, StorageConfig, User,
    )

    # Blueprints
    from blueprints import (auth, downloads, events, participants,
                            photographers, photos, picker, search, stats, storage, users)
    for module in (auth, users, events, photographers, participants, photos,
                   picker, search, downloads, stats, storage):
        app.register_blueprint(module.bp)


    @app.get("/api/health")
    def health():
        return jsonify({
            "status": "ok",
            "app": "Rubicon backend",
            "storageBackend": Config.STORAGE_BACKEND,
            "faceModel": Config.FACE_MODEL,
            "embedDim": Config.FACE_EMBED_DIM,
            "googleOAuth": bool(Config.GOOGLE_CLIENT_ID),
        })

    @app.get("/media/<event_id>/<path:name>")
    def media(event_id, name):
        base = os.path.abspath(os.path.join(Config.STORAGE_LOCAL_DIR, event_id))
        return send_from_directory(base, name)

    @app.get("/media/img/<key>/<variant>")
    def media_drive(key, variant):
        """Stream a Drive-backed photo derivative. Public, like the local /media
        route (participants are unauthenticated). `key` is the photo's storage key
        (google_media_id); `variant` is orig | web | thumb.

        Falls back to the event's active account when the photo's own account
        has a revoked/missing token (e.g. after a Drive reconnection).
        """
        from flask import Response, abort, stream_with_context

        from models import Photo, StorageAccount
        from services import gdrive

        if variant not in ("orig", "web", "thumb"):
            abort(404)
        photo = Photo.query.filter_by(google_media_id=key).first()
        if not photo or not photo.storage_account_id or not photo.storage_meta:
            abort(404)
        file_id = photo.storage_meta.get(variant)
        if not file_id:
            abort(404)

        account = db.session.get(StorageAccount, photo.storage_account_id)

        upstream = None
        working_account = None

        # 1. Try the photo's own account first (if it has a token)
        if account and account.refresh_token:
            try:
                token = gdrive.access_token_for(account.refresh_token)
                upstream = gdrive.download_stream(token, file_id)
                working_account = account
            except gdrive.DriveError:
                upstream = None

        # 2. Fallback: try ANY active account for this event
        #    (covers archived/disconnected accounts with revoked tokens)
        if upstream is None:
            fallback = (
                StorageAccount.query
                .filter_by(event_id=photo.event_id, provider="gdrive", status="active")
                .filter(StorageAccount.refresh_token.isnot(None))
                .first()
            )
            if fallback and fallback.refresh_token:
                try:
                    token = gdrive.access_token_for(fallback.refresh_token)
                    upstream = gdrive.download_stream(token, file_id)
                    working_account = fallback
                    # Backfill the working token onto the photo's account so
                    # future requests hit path 1 directly (self-healing).
                    if account:
                        account.refresh_token = fallback.refresh_token
                        db.session.commit()
                except gdrive.DriveError:
                    upstream = None

        if upstream is None:
            current_app.logger.warning(
                "Drive media proxy failed for all accounts: key=%s variant=%s photo_id=%s",
                key, variant, photo.id,
            )
            abort(502)

        resp = Response(
            stream_with_context(upstream.iter_content(chunk_size=65536)),
            content_type=upstream.headers.get("Content-Type", "image/jpeg"),
        )
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp

    @app.cli.command("init-db")
    def init_db():
        """Create all tables. Face vectors are stored as native float arrays
        and searched in NumPy, so no pgvector extension is required."""
        db.create_all()
        click.echo("Tables created.")

    @app.cli.command("upgrade-storage")
    def upgrade_storage():
        """Non-destructive upgrade for per-event Drive storage: create the
        storage_account table and add the two nullable columns to `photos`.
        Existing local photos/events are preserved (new columns default to NULL,
        which the code treats as legacy local storage)."""
        from sqlalchemy import text
        db.create_all()  # creates storage_account (and any other missing tables)
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE photos ADD COLUMN IF NOT EXISTS storage_account_id VARCHAR"))
            conn.execute(text("ALTER TABLE photos ADD COLUMN IF NOT EXISTS storage_meta JSON"))
        click.echo("Storage schema upgraded: storage_account table + photos.storage_account_id/storage_meta.")

    @app.cli.command("seed")
    def seed_cmd():
        """Load demo admin, event, photographers and participants."""
        from seed import run_seed
        run_seed()
        click.echo("Seed complete. Login: admin@rubicon.io / rubicon123")

    @app.cli.command("ingest-zips")
    @click.option("--dir", default=r"C:\Users\LENOVO\Downloads", help="Directory containing ZIP archives")
    @click.option("--event", default="evt-abia-2026", help="Event ID")
    @click.option("--pattern", default="ASLA*.zip", help="ZIP file glob pattern")
    def ingest_zips_cmd(dir, event, pattern):
        """Batch ingest photos from ZIP archives without manual extraction."""
        from ingest_zips import batch_ingest_zips
        batch_ingest_zips(downloads_dir=dir, event_id=event, pattern=pattern)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=Config.DEBUG)
