"""Local policy file storage: validated upload, immutable versioned storage.

Files never trust the client-supplied filename as a path. Every upload is
written to a temporary file inside GRC_DATA_DIR/tmp, validated by content
(not just extension), then atomically moved into
GRC_DATA_DIR/policies/<policy id>/<version number>/document.<ext> — a path
built entirely from server-generated ids, never the original filename.

Both entry points (`save_policy_version_upload` for a browser upload,
`save_policy_version_from_bytes` for Drive-captured content) share the same
write/validate/store core (`_save_policy_version`) — the only difference is
where the byte chunks come from.
"""

from __future__ import annotations

import hashlib
import os
import re
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from fastapi import UploadFile

ALLOWED_EXTENSIONS = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

_CHUNK_SIZE = 1024 * 1024


class UploadValidationError(ValueError):
    """A user-facing, safe-to-display reason an upload was rejected."""


@dataclass(frozen=True)
class StoredUpload:
    stored_filename: str
    media_type: str
    byte_size: int
    sha256: str
    original_filename: str


def sanitize_original_filename(filename: str) -> str:
    """Keep the original filename only for display/download — never as a path."""
    base = os.path.basename(filename or "").strip()
    base = base.replace("\x00", "")
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base)
    return base[:255] or "file"


def _extension_of(filename: str) -> str:
    _, _, ext = filename.rpartition(".")
    return ext.lower()


def _looks_like_pdf(path: str) -> bool:
    with open(path, "rb") as fh:
        header = fh.read(1024)
    return header.lstrip(b"\x00").startswith(b"%PDF-")


def _looks_like_docx(path: str) -> bool:
    if not zipfile.is_zipfile(path):
        return False
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
    return "word/document.xml" in names and "[Content_Types].xml" in names


def _validate_content(path: str, extension: str) -> None:
    if extension == "pdf" and not _looks_like_pdf(path):
        raise UploadValidationError("File does not look like a valid PDF.")
    if extension == "docx" and not _looks_like_docx(path):
        raise UploadValidationError("File does not look like a valid DOCX document.")


def _iter_upload_chunks(upload: UploadFile) -> Iterator[bytes]:
    while chunk := upload.file.read(_CHUNK_SIZE):
        yield chunk


def _iter_bytes_chunks(raw_bytes: bytes) -> Iterator[bytes]:
    for offset in range(0, len(raw_bytes), _CHUNK_SIZE):
        yield raw_bytes[offset : offset + _CHUNK_SIZE]


def _save_policy_version(
    chunks: Iterable[bytes],
    *,
    original_filename: str,
    extension: str,
    data_dir: str,
    policy_id: str,
    version_number: int,
    max_bytes: int,
) -> StoredUpload:
    """Write chunks to a temp file while hashing/bounding, validate content,
    then atomically move into the immutable version directory.

    Raises UploadValidationError for any rejected content; the temp file is
    always cleaned up before this function returns or raises.
    """
    tmp_dir = os.path.join(data_dir, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = os.path.join(tmp_dir, f"upload-{os.urandom(16).hex()}.part")

    sha256 = hashlib.sha256()
    byte_size = 0
    try:
        with open(tmp_path, "wb") as tmp_file:
            for chunk in chunks:
                byte_size += len(chunk)
                if byte_size > max_bytes:
                    raise UploadValidationError(
                        f"File exceeds the maximum upload size of {max_bytes // (1024 * 1024)} MB."
                    )
                sha256.update(chunk)
                tmp_file.write(chunk)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())

        _validate_content(tmp_path, extension)

        version_dir = os.path.join(data_dir, "policies", policy_id, str(version_number))
        os.makedirs(version_dir, exist_ok=True)
        stored_filename = f"document.{extension}"
        final_path = os.path.join(version_dir, stored_filename)
        if os.path.exists(final_path):
            raise UploadValidationError("This version already has a stored file.")
        os.replace(tmp_path, final_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    return StoredUpload(
        stored_filename=stored_filename,
        media_type=ALLOWED_EXTENSIONS[extension],
        byte_size=byte_size,
        sha256=sha256.hexdigest(),
        original_filename=original_filename,
    )


def save_policy_version_upload(
    upload: UploadFile,
    *,
    data_dir: str,
    policy_id: str,
    version_number: int,
    max_bytes: int,
) -> StoredUpload:
    """Validate and durably store one policy version from a browser upload."""
    original_filename = sanitize_original_filename(upload.filename or "")
    extension = _extension_of(original_filename)
    if extension not in ALLOWED_EXTENSIONS:
        raise UploadValidationError("Only PDF and DOCX files are accepted.")

    return _save_policy_version(
        _iter_upload_chunks(upload),
        original_filename=original_filename,
        extension=extension,
        data_dir=data_dir,
        policy_id=policy_id,
        version_number=version_number,
        max_bytes=max_bytes,
    )


def save_policy_version_from_bytes(
    raw_bytes: bytes,
    *,
    original_filename: str,
    data_dir: str,
    policy_id: str,
    version_number: int,
    max_bytes: int,
) -> StoredUpload:
    """Validate and durably store one policy version from in-memory bytes
    (e.g. content downloaded/exported from Google Drive)."""
    original_filename = sanitize_original_filename(original_filename)
    extension = _extension_of(original_filename)
    if extension not in ALLOWED_EXTENSIONS:
        raise UploadValidationError("Only PDF and DOCX files are accepted.")

    return _save_policy_version(
        _iter_bytes_chunks(raw_bytes),
        original_filename=original_filename,
        extension=extension,
        data_dir=data_dir,
        policy_id=policy_id,
        version_number=version_number,
        max_bytes=max_bytes,
    )


def policy_version_path(data_dir: str, policy_id: str, version_number: int, stored_filename: str) -> str:
    return os.path.join(data_dir, "policies", policy_id, str(version_number), stored_filename)
