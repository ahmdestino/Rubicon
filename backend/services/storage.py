"""
Storage abstraction — the "Storage Provider Agnostic" layer from ArchitectureModal.

Uploaded originals + derived thumbnails are written through a single interface so
the face-intelligence layer never cares where bytes live. Two backends exist:

  * local  — bytes on disk, served by the /media/<event>/<name> route.
  * gdrive — bytes in an event's connected Google Drive account, served by the
             /media/img/<key>/<variant> proxy. Chosen per-event when that event
             has an active StorageAccount (see blueprints/storage.py).

`save_image` picks the backend from the event's active account; `delete_image`
routes deletion back to whichever account holds the photo, so old photos keep
working after an event switches Drive accounts.
"""
import gc
import io
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

from PIL import Image, ImageOps

from config import Config
from models import StorageAccount
from services import gdrive
from services.ids import new_id

THUMB_MAX = 512
WEB_MAX   = 1600
# No cap on originals — full camera resolution is preserved.
# gc.collect() between encodes keeps peak RAM usage manageable.


class StorageResult:
    def __init__(self, key, url, high_res_url, thumbnail_url, width, height,
                 storage_account_id=None, storage_meta=None, exif=None):
        self.key = key
        self.url = url
        self.high_res_url = high_res_url
        self.thumbnail_url = thumbnail_url
        self.width = width
        self.height = height
        self.storage_account_id = storage_account_id
        self.storage_meta = storage_meta
        self.exif = exif


def active_account(event_id: str):
    """The event's current active storage account, or None (→ local default)."""
    return (StorageAccount.query
            .filter_by(event_id=event_id, status="active", connected=True)
            .first())


def _local_dir(event_id: str) -> str:
    path = os.path.join(Config.STORAGE_LOCAL_DIR, event_id)
    os.makedirs(path, exist_ok=True)
    return path


def _resize(img: Image.Image, max_side: int) -> Image.Image:
    w, h = img.size
    if max(w, h) <= max_side:
        return img.copy()
    scale = max_side / float(max(w, h))
    return img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


def _encode(img: Image.Image, max_side: int | None, quality: int) -> bytes:
    buf = io.BytesIO()
    resized = img if max_side is None else _resize(img, max_side)
    resized.save(buf, "JPEG", quality=quality, optimize=False)
    data = buf.getvalue()
    buf.close()
    del buf
    gc.collect()
    return data


# --- EXIF extraction -------------------------------------------------------
# Camera bodies stamp shot settings + the real capture time into EXIF. We read
# the fields the UI shows (ISO / shutter / aperture / focal length) and — the
# point of the exercise — DateTimeOriginal, the instant the shutter fired,
# normalized to ISO 8601 so the gallery can sort by "date taken" (not upload
# time). Best-effort: any image without usable EXIF just yields None.
_EXIF_OFFSET = 0x8769
_DATE_TIME_ORIGINAL = 0x9003
_DATE_TIME_DIGITIZED = 0x9004
_DATE_TIME = 0x0132
_ISO = 0x8827
_EXPOSURE_TIME = 0x829A
_F_NUMBER = 0x829D
_FOCAL_LENGTH = 0x920A


def _as_float(v):
    try:
        f = float(v)
        return f if f == f else None  # reject NaN
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _exif_datetime_to_iso(raw):
    """EXIF stamps time as 'YYYY:MM:DD HH:MM:SS'. Convert to ISO 8601 so the
    frontend's `new Date(...)` parses it; return None if unusable."""
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw or raw.startswith("0000"):
        return None
    date_part, _, time_part = raw.partition(" ")
    iso = date_part.replace(":", "-")
    if time_part:
        iso += "T" + time_part.strip()
    try:
        datetime.fromisoformat(iso)
    except ValueError:
        return None
    return iso


def _fmt_shutter(v):
    f = _as_float(v)
    if not f or f <= 0:
        return None
    return f"{f:g}s" if f >= 1 else f"1/{round(1 / f)}s"


def _extract_exif(img: Image.Image):
    """Return a dict of only the populated EXIF fields, or None. Never raises."""
    try:
        exif = img.getexif()
    except Exception:
        return None
    if not exif:
        return None
    try:
        sub = exif.get_ifd(_EXIF_OFFSET)
    except Exception:
        sub = {}

    iso = sub.get(_ISO)
    if isinstance(iso, (list, tuple)):
        iso = iso[0] if iso else None
    fnum = _as_float(sub.get(_F_NUMBER))
    focal = _as_float(sub.get(_FOCAL_LENGTH))

    out = {
        "capturedAt": _exif_datetime_to_iso(
            sub.get(_DATE_TIME_ORIGINAL)
            or sub.get(_DATE_TIME_DIGITIZED)
            or exif.get(_DATE_TIME)
        ),
        "iso": str(iso) if iso not in (None, "") else None,
        "shutter": _fmt_shutter(sub.get(_EXPOSURE_TIME)),
        "aperture": f"f/{fnum:g}" if fnum and fnum > 0 else None,
        "focalLength": f"{focal:g}mm" if focal and focal > 0 else None,
    }
    out = {k: v for k, v in out.items() if v}
    return out or None


def save_image(raw: bytes, event_id: str, filename: str) -> StorageResult:
    """Persist original + web + thumbnail derivatives, return their URLs + dims.
    Uploads to the event's active Google Drive account when one is connected,
    otherwise falls back to local disk (unchanged behavior).

    Large DSLR images (45 MP+) are capped at ORIG_MAX on the longest side so
    Pillow does not exhaust the process heap while encoding the JPEG buffers.
    """
    # Raise Pillow's decompression-bomb threshold for legitimate large photos
    Image.MAX_IMAGE_PIXELS = 250_000_000   # ~250 MP; blocks truly malicious files

    img = Image.open(io.BytesIO(raw))
    exif = _extract_exif(img)           # read metadata before RGB-flattening drops it
    img = ImageOps.exif_transpose(img)  # rotate pixel matrix per EXIF orientation tag
    img = img.convert("RGB")
    width, height = img.size
    key = new_id("gphotos_media")

    try:
        orig_bytes  = _encode(img, None,      95)  # full original resolution
        web_bytes   = _encode(img, WEB_MAX,   88)
        thumb_bytes = _encode(img, THUMB_MAX, 80)
    except (MemoryError, OSError) as exc:
        # Last-resort fallback: shrink to 4000px and retry once
        gc.collect()
        img = _resize(img, 4000)
        try:
            orig_bytes  = _encode(img, None,      92)
            web_bytes   = _encode(img, WEB_MAX,   85)
            thumb_bytes = _encode(img, THUMB_MAX, 78)
        except (MemoryError, OSError):
            raise MemoryError(
                f"Image too large to process even at 4000px "
                f"({width}x{height} px, {len(raw)//1024} KB raw): {exc}"
            ) from exc
    finally:
        del img
        gc.collect()

    account = active_account(event_id)
    if account and account.provider == "gdrive":
        result = _save_gdrive(account, key, filename, width, height,
                              orig_bytes, web_bytes, thumb_bytes)
    else:
        result = _save_local(event_id, key, width, height, orig_bytes, web_bytes, thumb_bytes)
    result.exif = exif
    return result


def _save_local(event_id, key, width, height, orig_bytes, web_bytes, thumb_bytes) -> StorageResult:
    base = _local_dir(event_id)
    names = {"orig": f"{key}.jpg", "web": f"{key}_web.jpg", "thumb": f"{key}_thumb.jpg"}
    for name, data in ((names["orig"], orig_bytes), (names["web"], web_bytes), (names["thumb"], thumb_bytes)):
        with open(os.path.join(base, name), "wb") as fh:
            fh.write(data)

    def url_for(name):
        return f"{Config.MEDIA_BASE_URL}/{event_id}/{name}"

    return StorageResult(
        key=key, url=url_for(names["web"]), high_res_url=url_for(names["orig"]),
        thumbnail_url=url_for(names["thumb"]), width=width, height=height,
    )


def _save_gdrive(account, key, filename, width, height,
                 orig_bytes, web_bytes, thumb_bytes) -> StorageResult:
    token = gdrive.access_token_for(account.refresh_token)
    stem = os.path.splitext(os.path.basename(filename or key))[0]
    meta = {
        "orig": gdrive.upload_file(token, account.root_folder_id, f"{stem}__{key}.jpg", orig_bytes),
        "web": gdrive.upload_file(token, account.root_folder_id, f"{stem}__{key}_web.jpg", web_bytes),
        "thumb": gdrive.upload_file(token, account.root_folder_id, f"{stem}__{key}_thumb.jpg", thumb_bytes),
    }

    def url_for(variant):
        return f"{Config.MEDIA_BASE_URL}/img/{key}/{variant}"

    return StorageResult(
        key=key, url=url_for("web"), high_res_url=url_for("orig"),
        thumbnail_url=url_for("thumb"), width=width, height=height,
        storage_account_id=account.id, storage_meta=meta,
    )


def local_path(event_id: str, name: str) -> str:
    return os.path.join(Config.STORAGE_LOCAL_DIR, event_id, name)


def delete_image(photo) -> None:
    """Remove a photo's derivatives from wherever they live. Idempotent: missing
    files / already-deleted Drive objects are ignored."""
    if photo is None:
        return
    if photo.storage_account_id and photo.storage_meta:
        account = StorageAccount.query.get(photo.storage_account_id)
        if account and account.provider == "gdrive" and account.refresh_token:
            try:
                token = gdrive.access_token_for(account.refresh_token)
                for file_id in photo.storage_meta.values():
                    if file_id:
                        gdrive.delete_file(token, file_id)
            except Exception:
                pass
        return

    key = photo.google_media_id
    if not key:
        return
    base = os.path.join(Config.STORAGE_LOCAL_DIR, photo.event_id)
    for name in (f"{key}.jpg", f"{key}_web.jpg", f"{key}_thumb.jpg"):
        try:
            os.remove(os.path.join(base, name))
        except FileNotFoundError:
            pass
