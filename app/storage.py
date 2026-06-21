"""Object storage helpers.

Mirrors the four private Supabase Storage buckets:
  redacted-images, output-sheets, corrected-uploads, reference-logs

In OFFLINE_MODE the same calls write under LOCAL_DATA_DIR/storage/<bucket>/ so
the pipeline runs end to end without a network. Raw images are never persisted
by anything in this module — callers hand us only redacted bytes.
"""
from __future__ import annotations

from pathlib import Path

from app.config import settings
from app.db import db

REDACTED_IMAGES = "redacted-images"
OUTPUT_SHEETS = "output-sheets"
CORRECTED_UPLOADS = "corrected-uploads"
REFERENCE_LOGS = "reference-logs"


class _LocalStorage:
    def __init__(self, root: str):
        self.root = Path(root) / "storage"

    def _p(self, bucket: str, path: str) -> Path:
        full = self.root / bucket / path
        full.parent.mkdir(parents=True, exist_ok=True)
        return full

    def put(self, bucket: str, path: str, data: bytes, content_type: str) -> str:
        self._p(bucket, path).write_bytes(data)
        return f"{bucket}/{path}"

    def get(self, bucket: str, path: str) -> bytes:
        return self._p(bucket, path).read_bytes()

    def delete(self, bucket: str, path: str) -> None:
        p = self._p(bucket, path)
        if p.exists():
            p.unlink()


class _SupabaseStorage:
    def __init__(self):
        self.client = db.backend.client  # reuse the configured service-role client

    def put(self, bucket: str, path: str, data: bytes, content_type: str) -> str:
        # supabase-py 2.x (storage-3x-python): upsert must be the Python bool
        # True (sent as the x-upsert header); "true" (string) is rejected.
        # If the file already exists the upsert:True path overwrites silently.
        self.client.storage.from_(bucket).upload(
            path,
            data,
            {"content-type": content_type, "upsert": True},
        )
        return f"{bucket}/{path}"

    def get(self, bucket: str, path: str) -> bytes:
        return self.client.storage.from_(bucket).download(path)

    def delete(self, bucket: str, path: str) -> None:
        self.client.storage.from_(bucket).remove([path])


_backend = _LocalStorage(settings.local_data_dir) if db.offline else _SupabaseStorage()


def put_object(bucket: str, path: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Store bytes; returns the 'bucket/path' storage reference."""
    return _backend.put(bucket, path, data, content_type)


def get_object(bucket: str, path: str) -> bytes:
    return _backend.get(bucket, path)


def delete_object(bucket: str, path: str) -> None:
    _backend.delete(bucket, path)


def split_ref(storage_ref: str) -> tuple[str, str]:
    """'bucket/some/path.png' -> ('bucket', 'some/path.png')."""
    bucket, _, path = storage_ref.partition("/")
    return bucket, path
