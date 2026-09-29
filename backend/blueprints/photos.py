"""Photos: gallery listing, detail, view counter, real upload+ingest, job polling."""
import requests as http
from flask import Blueprint, jsonify, request

from sqlalchemy.orm import joinedload, selectinload
from auth_utils import admin_required
from extensions import db
from models import Event, FaceDetection, IngestionJob, Photo
from services.ids import new_id
from services.ingest import ingest_photo
from services.storage import delete_image

bp = Blueprint("photos", __name__, url_prefix="/api")


@bp.get("/events/<event_id>/photos")
def list_photos(event_id):
    session = request.args.get("session")
    photographer_id = request.args.get("photographerId")
    q = (request.args.get("q") or "").strip().lower()
    sort = request.args.get("sort", "newest")
    page = max(1, int(request.args.get("page", 1)))
    page_size = min(500, max(1, int(request.args.get("pageSize", 24))))

    query = (
        Photo.query
        .options(
            selectinload(Photo.faces).selectinload(FaceDetection.participant),
            joinedload(Photo.photographer),
        )
        .filter_by(event_id=event_id)
    )
    if session and session != "All Sessions":
        query = query.filter(Photo.session_tag == session)
    if photographer_id:
        query = query.filter(Photo.photographer_id == photographer_id)

    if sort == "popular":
        query = query.order_by(Photo.view_count.desc())
    elif sort == "downloads":
        query = query.order_by(Photo.download_count.desc())
    else:
        query = query.order_by(Photo.uploaded_at.desc())

    if q:
        # Text search can't be pushed to DB without full-text indexes —
        # load all matching rows, filter in Python, then re-paginate.
        rows = query.all()
        def match(p):
            hay = " ".join([
                p.filename or "", p.session_tag or "",
                p.photographer.name if p.photographer else "",
                " ".join(f.participant.name for f in p.faces if f.participant),
            ]).lower()
            return q in hay
        rows = [p for p in rows if match(p)]
        total = len(rows)
        start = (page - 1) * page_size
        page_rows = rows[start:start + page_size]
    else:
        # DB-level pagination: only fetch the requested page — fast for large events.
        total = query.count()
        page_rows = query.offset((page - 1) * page_size).limit(page_size).all()

    import math
    total_pages = math.ceil(total / page_size) if total > 0 else 1
    return jsonify({
        "items": [p.to_dict() for p in page_rows],
        "total": total,
        "page": page,
        "pageSize": page_size,
        "totalPages": total_pages,
    })


@bp.get("/photos/<pid>")
def get_photo(pid):
    p = db.session.get(Photo, pid)
    return (jsonify(p.to_dict()), 200) if p else (jsonify({"error": "not found"}), 404)


@bp.delete("/photos/<pid>")
@admin_required
def delete_photo(pid):
    p = db.session.get(Photo, pid)
    if not p:
        return jsonify({"error": "not found"}), 404
    # Remove the stored derivatives (local disk or the photo's Drive account),
    # then the row (face rows cascade).
    delete_image(p)
    db.session.delete(p)
    db.session.commit()
    return jsonify({"ok": True})


@bp.post("/photos/<pid>/view")
def bump_view(pid):
    p = db.session.get(Photo, pid)
    if not p:
        return jsonify({"error": "not found"}), 404
    p.view_count += 1
    db.session.commit()
    return jsonify({"viewCount": p.view_count})


@bp.post("/events/<event_id>/photos")
@admin_required
def upload_photos(event_id):
    """multipart/form-data: files[] + photographerId + sessionTag. Runs the real
    detect+embed pipeline per file (synchronous) and returns the resulting jobs.
    Duplicate images (matched by SHA-256 hash or filename) are skipped — no
    second copy is created and the job is marked stage='skipped'."""
    if not db.session.get(Event, event_id):
        return jsonify({"error": "event not found"}), 404
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "no files uploaded"}), 400
    photographer_id = request.form.get("photographerId")
    session_tag = request.form.get("sessionTag", "General")
    camera_info = request.form.get("cameraInfo", "")

    jobs = []
    new_count = 0
    skipped_count = 0
    for f in files:
        job = IngestionJob(
            id=new_id("job"), filename=f.filename, photographer_id=photographer_id,
            event_id=event_id, session_tag=session_tag, stage="uploading_storage", progress=0,
        )
        db.session.add(job)
        db.session.commit()
        try:
            result = ingest_photo(event_id, photographer_id, session_tag, f.filename,
                         f.read(), camera_info=camera_info, job=job)
            if job.stage == "skipped":
                skipped_count += 1
            else:
                new_count += 1
        except Exception as exc:  # keep the batch going; surface per-file failure
            job.stage = "error"
            job.error = str(exc)
            db.session.commit()
        jobs.append(job.to_dict())

    return jsonify({"jobs": jobs, "new": new_count, "skipped": skipped_count}), 201


@bp.post("/events/<event_id>/photos/from-picker")
@admin_required
def upload_from_picker(event_id):
    """Ingest photos selected via Google Photos Picker session.
    Body: { sessionId: str, photographerId: str, sessionTag: str, cameraInfo: str }
    """
    if not db.session.get(Event, event_id):
        return jsonify({"error": "event not found"}), 404

    data = request.get_json(silent=True) or {}
    session_id = (data.get("sessionId") or "").strip()
    photographer_id = data.get("photographerId")
    session_tag = data.get("sessionTag", "General")
    camera_info = data.get("cameraInfo", "Google Photos")

    if not session_id:
        return jsonify({"error": "sessionId is required"}), 400

    from blueprints.picker import PICKER_BASE, get_session_token
    token = get_session_token(session_id)
    if not token:
        return jsonify({"error": "Picker session expired or not found"}), 404

    # 1. Fetch picked items list from Google Photos Picker API
    try:
        resp = http.get(
            f"{PICKER_BASE}/mediaItems",
            params={"sessionId": session_id, "pageSize": 100},
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        resp.raise_for_status()
    except Exception as exc:
        return jsonify({"error": f"Failed to list picked media items: {exc}"}), 502

    items_data = resp.json().get("mediaItems", [])
    if not items_data:
        return jsonify({"jobs": [], "message": "No media items selected"}), 200

    jobs = []
    new_count = 0
    skipped_count = 0
    for item in items_data:
        media_file = item.get("mediaFile") or item
        base_url = media_file.get("baseUrl")
        filename = media_file.get("filename") or f"gphoto_{item.get('id', new_id('gp'))}.jpg"

        if not base_url:
            continue

        job = IngestionJob(
            id=new_id("job"),
            filename=filename,
            photographer_id=photographer_id,
            event_id=event_id,
            session_tag=session_tag,
            stage="downloading_google_photos",
            progress=10,
        )
        db.session.add(job)
        db.session.commit()

        try:
            # Download full-res bytes from baseUrl (with =d parameter for full download)
            download_url = f"{base_url}=d"
            dl_resp = http.get(
                download_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            dl_resp.raise_for_status()
            raw_bytes = dl_resp.content

            # Ingest into storage + face recognition (skips if duplicate)
            ingest_photo(
                event_id=event_id,
                photographer_id=photographer_id,
                session_tag=session_tag,
                filename=filename,
                raw=raw_bytes,
                camera_info=camera_info,
                job=job,
            )
            if job.stage == "skipped":
                skipped_count += 1
            else:
                new_count += 1
        except Exception as exc:
            job.stage = "error"
            job.error = str(exc)
            db.session.commit()

        jobs.append(job.to_dict())

    return jsonify({"jobs": jobs, "new": new_count, "skipped": skipped_count}), 201


@bp.get("/ingestion-jobs/<jid>")
def get_job(jid):
    job = db.session.get(IngestionJob, jid)
    return (jsonify(job.to_dict()), 200) if job else (jsonify({"error": "not found"}), 404)
