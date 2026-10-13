"""
Face detection + embedding via the remote Face API (Server 2).

detect_faces() / embed_selfie() are the public interface used by
services/ingest.py and blueprints/search.py. Embeddings are 512-d and
L2-normalized, so cosine similarity == dot product (services/search.py).

All ML inference (InsightFace / ONNX) runs on the dedicated Face API service.
This server only handles HTTP transport and NumPy embedding construction.
"""
import io

import numpy as np
import requests
from PIL import Image, ImageOps

from config import Config


class DetectedFace:
    def __init__(self, embedding, box_pct, det_score):
        self.embedding = embedding      # np.ndarray (dim,), L2-normalized
        self.box = box_pct              # dict x,y,width,height as percentages 0..100
        self.confidence = det_score     # detector confidence 0..1


class FaceServiceError(RuntimeError):
    """The remote face service could not process an image."""


def warm_up():
    """No-op: inference runs on the remote Face API, nothing to warm here."""
    return


def _to_rgb_bytes(raw: bytes) -> bytes:
    """Downscale oversized images before sending to the Face API to cap payload size."""
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img).convert("RGB")
    max_dim = 640
    if max(img.width, img.height) > max_dim:
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue()
    return raw


def _detect_remote(raw: bytes):
    """Use the isolated Face API while preserving the local result contract."""
    if not Config.FACE_API_TOKEN:
        raise FaceServiceError("FACE_API_TOKEN must be set when FACE_API_URL is configured")

    payload = _to_rgb_bytes(raw)

    try:
        response = requests.post(
            f"{Config.FACE_API_URL}/detect",
            files={"image": ("image.jpg", payload, "application/octet-stream")},
            headers={"X-Face-Api-Token": Config.FACE_API_TOKEN},
            timeout=Config.FACE_API_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise FaceServiceError("Face API is unavailable") from exc

    if not response.ok:
        try:
            message = response.json().get("error", response.text)
        except ValueError:
            message = response.text
        raise FaceServiceError(f"Face API rejected the image: {message}")

    try:
        data = response.json()
        return [
            DetectedFace(
                np.asarray(face["embedding"], dtype=np.float32),
                face["box"],
                float(face["confidence"]),
            )
            for face in data["faces"]
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise FaceServiceError("Face API returned an invalid response") from exc


def detect_faces(raw: bytes):
    """Return a list[DetectedFace] for every face in the image bytes."""
    return _detect_remote(raw)


def embed_selfie(raw: bytes):
    """Embed the single most prominent face in a selfie; None if no face found."""
    faces = detect_faces(raw)
    if not faces:
        return None
    # Largest box == closest / most prominent face.
    faces.sort(key=lambda f: f.box["width"] * f.box["height"], reverse=True)
    return faces[0].embedding
