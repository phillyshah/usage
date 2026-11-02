"""Object storage helpers.

Mirrors the four private Supabase Storage buckets:
  redacted-images, output-sheets, corrected-uploads, reference-logs

In OFFLINE_MODE the same calls write under LOCAL_DATA_DIR/storage/<bucket>/ so
the pipeline runs end to end without a network.

The ticket-image bucket is still named "redacted-images" on the server. The
name is history — images are no longer masked before storage — but renaming the
bucket would strand every image already in it, so only the Python constant
changed.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from app.config import settings
from app.db import db

log = logging.getLogger("usage.storage")

TICKET_IMAGES = "redacted-images"   # bucket id is historical; see above
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
        # storage3 2.x copies the `upsert` option straight into the `x-upsert`
        # HTTP header, so it must be the STRING "true" (httpx rejects a bool
        # header value with a TypeError). Upsert overwrites if the object exists.
        #
        # Timed and sized on every call, success or failure. "The upload timed
        # out" is not a fact anyone can act on; "290 KB did not complete in 30
        # seconds" says the connection is hanging rather than slow, and that is
        # a different problem with a different fix.
        started = time.monotonic()
        try:
            self.client.storage.from_(bucket).upload(
                path,
                data,
                {"content-type": content_type, "upsert": "true"},
            )
        except Exception as e:
            log.warning("storage upload FAILED after %.1fs: %s/%s %.0fKB (%s: %s)",
                        time.monotonic() - started, bucket, path,
                        len(data) / 1024, type(e).__name__, e)
            raise
        elapsed = time.monotonic() - started
        rate = (len(data) / 1024) / elapsed if elapsed > 0 else 0
        log.info("storage upload ok in %.1fs: %s/%s %.0fKB (%.0f KB/s)",
                 elapsed, bucket, path, len(data) / 1024, rate)
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
