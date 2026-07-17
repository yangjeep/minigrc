"""Shared bounded-read helper for size-limited uploads.

Extracted once a second caller (vendor roster CSV import) needed the same
chunked-read-with-a-cap pattern already used for framework CSV import —
see app/storage.py::save_policy_version_upload for the on-disk sibling of
this same idea.
"""

from __future__ import annotations

from fastapi import UploadFile

_CHUNK_SIZE = 1024 * 1024


class UploadTooLargeError(ValueError):
    """Raised once a bounded read exceeds its configured byte limit."""


def read_upload_bounded(upload: UploadFile, *, max_bytes: int) -> bytes:
    """Read an upload in bounded chunks, never buffering past max_bytes.

    Raises UploadTooLargeError before the full file is read into memory,
    so an oversized upload never reaches a parser or the database.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := upload.file.read(_CHUNK_SIZE):
        total += len(chunk)
        if total > max_bytes:
            raise UploadTooLargeError(
                f"File exceeds the maximum upload size of {max_bytes // (1024 * 1024)} MB."
            )
        chunks.append(chunk)
    return b"".join(chunks)
