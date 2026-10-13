"""
Vector search over stored face embeddings using NumPy cosine similarity.

Embeddings are stored as native PostgreSQL float arrays; at event scale a
brute-force cosine scan in NumPy is fast enough and needs no database
extension.

Memory strategy: we SELECT only (id, embedding) — not full ORM objects — and
stream rows in chunks so SQLAlchemy never materialises the whole event's face
table into Python objects at once.
"""
import numpy as np
from sqlalchemy import select, text

from config import Config
from extensions import db
from models import FaceDetection, Photo

_CHUNK = 500  # rows fetched per round-trip; tune to stay under memory budget


def search_by_embedding(event_id: str, embedding: np.ndarray, limit: int = 60,
                        threshold: float = None):
    """
    Return [(FaceDetection, similarity)] whose faces are the nearest neighbours of
    `embedding` within an event, filtered by cosine-similarity threshold and sorted
    best-first. similarity = 1 - cosine_distance.

    Only (id, embedding) columns are streamed from the DB; full FaceDetection
    objects are fetched only for the rows that pass the threshold, keeping peak
    RAM proportional to matches rather than to the entire event's face table.
    """
    if threshold is None:
        threshold = Config.FACE_MATCH_THRESHOLD

    query_vec = np.asarray(embedding, dtype=np.float32).ravel()
    query_norm = np.linalg.norm(query_vec)
    if query_norm == 0:
        return []
    query_vec = query_vec / query_norm

    # Lightweight query: only two columns, no ORM object construction.
    stmt = (
        select(FaceDetection.id, FaceDetection.embedding)
        .join(Photo, FaceDetection.photo_id == Photo.id)
        .filter(Photo.event_id == event_id)
        .filter(FaceDetection.embedding.isnot(None))
        .execution_options(stream_results=True)
    )

    candidates = []  # list of (face_id, similarity)

    with db.engine.connect() as conn:
        result = conn.execute(stmt)
        while True:
            chunk = result.fetchmany(_CHUNK)
            if not chunk:
                break
            ids = [row[0] for row in chunk]
            vecs = [row[1] for row in chunk if row[1] and len(row[1]) > 0]
            if not vecs:
                continue

            matrix = np.array(vecs, dtype=np.float32)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            sims = np.dot(matrix / norms, query_vec)

            for face_id, sim in zip(ids, sims):
                if sim >= threshold:
                    candidates.append((face_id, float(sim)))

    if not candidates:
        return []

    # Sort and take top-k, then fetch only the winning rows as full ORM objects.
    candidates.sort(key=lambda x: x[1], reverse=True)
    top = candidates[:limit]
    top_ids = [fid for fid, _ in top]
    sim_map = {fid: sim for fid, sim in top}

    faces = db.session.query(FaceDetection).filter(FaceDetection.id.in_(top_ids)).all()
    return [(f, sim_map[f.id]) for f in faces]
