"""S3-compatible object storage for the evidence artifact repository
(issue #32).

Mirrors app/storage.py's shape: stream to a local temp file while
hashing/bounding, validate, *then* commit — here "commit" means a
single `put_object`-equivalent upload rather than an atomic local
`os.replace`. See
docs/superpowers/specs/2026-08-19-issue32-evidence-repository-design.md
§4 for the full design, including why uploads are proxied through the
app rather than presigned-URL redirects, and why S3 upload must fully
complete before any DomainEvent/DB row is created.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import boto3
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import UploadFile

from app.config import Settings

_CHUNK_SIZE = 1024 * 1024

# Broader than app/storage.py's policy-document set (pdf/docx only) —
# evidence realistically includes screenshots and plain data exports
# too. Content-sniffed where a magic-byte signature exists (pdf/docx/
# png/jpeg, mirroring app/storage.py's own precedent); the plain-text
# formats have no meaningful signature to check and are trusted by
# extension only, same risk category as any downloaded-not-executed
# file.
ALLOWED_EVIDENCE_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "csv": "text/csv",
    "txt": "text/plain",
    "json": "application/json",
    "log": "text/plain",
}
_SNIFFED_EXTENSIONS = frozenset({"pdf", "docx", "png", "jpg", "jpeg"})


class ObjectStorageError(ValueError):
    """A user-facing, safe-to-display reason a capture/upload/download
    was rejected — never includes credential material."""


class ObjectStorageNotConfiguredError(ObjectStorageError):
    """Raised when settings.evidence_repository_configured is False."""


@dataclass(frozen=True)
class CapturedBytes:
    tmp_path: str
    sha256: str
    byte_size: int


def extension_of(filename: str) -> str:
    _, _, ext = filename.rpartition(".")
    return ext.lower()


def _looks_like_pdf(path: str) -> bool:
    with open(path, "rb") as fh:
        header = fh.read(1024)
    return header.lstrip(b"\x00").startswith(b"%PDF-")


def _looks_like_png(path: str) -> bool:
    with open(path, "rb") as fh:
        header = fh.read(8)
    return header == b"\x89PNG\r\n\x1a\n"


def _looks_like_jpeg(path: str) -> bool:
    with open(path, "rb") as fh:
        header = fh.read(3)
    return header == b"\xff\xd8\xff"


def _looks_like_docx(path: str) -> bool:
    if not zipfile.is_zipfile(path):
        return False
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
    return "word/document.xml" in names and "[Content_Types].xml" in names


_SNIFFERS = {
    "pdf": _looks_like_pdf,
    "docx": _looks_like_docx,
    "png": _looks_like_png,
    "jpg": _looks_like_jpeg,
    "jpeg": _looks_like_jpeg,
}


def validate_evidence_content(path: str, extension: str) -> None:
    """Content-sniff the extensions that have a real magic-byte
    signature (see _SNIFFED_EXTENSIONS); plain-text formats have
    nothing meaningful to sniff and are trusted by extension alone —
    same risk category as any other downloaded-not-executed file."""
    if extension in _SNIFFED_EXTENSIONS and not _SNIFFERS[extension](path):
        raise ObjectStorageError(f"File does not look like a valid .{extension} file.")


def _iter_upload_chunks(upload: UploadFile) -> Iterator[bytes]:
    while chunk := upload.file.read(_CHUNK_SIZE):
        yield chunk


def _iter_bytes_chunks(raw_bytes: bytes) -> Iterator[bytes]:
    for offset in range(0, len(raw_bytes), _CHUNK_SIZE):
        yield raw_bytes[offset : offset + _CHUNK_SIZE]


def _capture_chunks_to_temp_file(chunks: Iterable[bytes], *, max_bytes: int) -> CapturedBytes:
    """Stream chunks to a local temp file while hashing/bounding — same
    core pattern as app/storage.py::_save_policy_version, staged for an
    S3 upload rather than a final local path. Caller is responsible for
    removing `tmp_path` once it has been uploaded (or on any later
    failure) — see upload_evidence_version in app/evidence_repository.py.
    """
    fd, tmp_path = tempfile.mkstemp(prefix="evidence-upload-")
    sha256 = hashlib.sha256()
    byte_size = 0
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            for chunk in chunks:
                byte_size += len(chunk)
                if byte_size > max_bytes:
                    raise ObjectStorageError(
                        f"File exceeds the maximum upload size of {max_bytes // (1024 * 1024)} MB."
                    )
                sha256.update(chunk)
                tmp_file.write(chunk)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    return CapturedBytes(tmp_path=tmp_path, sha256=sha256.hexdigest(), byte_size=byte_size)


def capture_upload_bytes(upload: UploadFile, *, max_bytes: int) -> CapturedBytes:
    """Stage a browser upload for an S3 commit."""
    return _capture_chunks_to_temp_file(_iter_upload_chunks(upload), max_bytes=max_bytes)


def capture_raw_bytes(raw_bytes: bytes, *, max_bytes: int) -> CapturedBytes:
    """Stage in-memory bytes (a future connector capture) for an S3 commit."""
    return _capture_chunks_to_temp_file(_iter_bytes_chunks(raw_bytes), max_bytes=max_bytes)


def build_s3_client(settings: Settings):
    """Construct a boto3 S3 client against the configured self-hosted
    (or any S3-compatible) endpoint. Raises ObjectStorageNotConfiguredError
    rather than constructing a client against empty credentials —
    callers must check this first so a misconfigured deployment fails
    with a clear message, not a confusing boto3 error deep in a route."""
    if not settings.evidence_repository_configured:
        raise ObjectStorageNotConfiguredError("The evidence artifact repository is not configured.")
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name=settings.s3_region,
    )


def object_key_for(artifact_id: str, version_id: str, extension: str) -> str:
    """Entirely server-generated — the original filename is never a
    path/key component (matches app/storage.py::sanitize_original_filename's
    same guarantee for local storage)."""
    safe_extension = re.sub(r"[^A-Za-z0-9]", "", extension)[:16] or "bin"
    return f"evidence/{artifact_id}/{version_id}/content.{safe_extension}"


def upload_object(client, *, bucket: str, object_key: str, tmp_path: str, media_type: str) -> None:
    try:
        client.upload_file(tmp_path, bucket, object_key, ExtraArgs={"ContentType": media_type})
    except (BotoCoreError, ClientError, S3UploadFailedError) as exc:
        # upload_file's threaded transfer manager wraps a ClientError in
        # its own S3UploadFailedError rather than letting it propagate
        # directly — found via a real moto-backed "bucket does not
        # exist" test, not assumed.
        raise ObjectStorageError(f"Evidence upload failed: {exc}") from exc


def delete_object(client, *, bucket: str, object_key: str) -> None:
    """Best-effort cleanup for an object that was successfully uploaded
    but whose subsequent DB/event write then failed (see design doc
    §4) — never raises. An unreferenced leftover object in the bucket
    is a containable, non-authoritative artifact; a DB row/event
    referencing a missing object would be a real correctness violation,
    which is what the upload-before-commit ordering this supports is
    designed to prevent. Failing the caller's original error on top of
    a cleanup failure would only obscure the real problem."""
    try:
        client.delete_object(Bucket=bucket, Key=object_key)
    except (BotoCoreError, ClientError):
        pass


def download_object(client, *, bucket: str, object_key: str) -> bytes:
    try:
        response = client.get_object(Bucket=bucket, Key=object_key)
        return response["Body"].read()
    except (BotoCoreError, ClientError) as exc:
        raise ObjectStorageError(f"Evidence download failed: {exc}") from exc
