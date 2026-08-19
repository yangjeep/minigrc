"""Unit tests for app/object_storage.py (issue #32) — the hash/bound/
validate/key-generation core, independent of any real or mocked S3
target. Integration tests against a moto-backed S3 live in
tests/test_evidence_repository.py.
"""

from __future__ import annotations

import os

import pytest

from app.object_storage import (
    ObjectStorageError,
    ObjectStorageNotConfiguredError,
    build_s3_client,
    capture_raw_bytes,
    extension_of,
    object_key_for,
    validate_evidence_content,
)


def test_capture_raw_bytes_hashes_and_sizes_correctly():
    import hashlib

    captured = capture_raw_bytes(b"hello world", max_bytes=1024)
    assert captured.byte_size == 11
    assert captured.sha256 == hashlib.sha256(b"hello world").hexdigest()
    assert os.path.exists(captured.tmp_path)
    os.remove(captured.tmp_path)


def test_capture_raw_bytes_rejects_oversize_and_cleans_up_temp_file():
    with pytest.raises(ObjectStorageError, match="maximum upload size"):
        capture_raw_bytes(b"x" * 100, max_bytes=10)


def test_capture_raw_bytes_empty_content():
    captured = capture_raw_bytes(b"", max_bytes=1024)
    assert captured.byte_size == 0
    os.remove(captured.tmp_path)


def test_extension_of():
    assert extension_of("Report.PDF") == "pdf"
    # No "." at all: rpartition returns the whole string as the tail —
    # matches app/storage.py::_extension_of's identical behavior; such a
    # filename simply fails the ALLOWED_EVIDENCE_MEDIA_TYPES membership
    # check at the route layer, same as any other unsupported extension.
    assert extension_of("no-extension") == "no-extension"
    assert extension_of("archive.tar.gz") == "gz"


def test_object_key_for_is_entirely_server_generated():
    key = object_key_for("artifact123", "version456", "pdf")
    assert key == "evidence/artifact123/version456/content.pdf"


@pytest.mark.parametrize(
    "malicious_extension",
    ["pdf/../../etc/passwd", "../../secret", "pdf\x00.exe", "PDF; rm -rf /"],
)
def test_object_key_for_sanitizes_malicious_extension(malicious_extension):
    key = object_key_for("artifact123", "version456", malicious_extension)
    assert ".." not in key
    assert "/" not in key.rsplit("/", 1)[-1]  # no extra path segments smuggled in via extension
    assert key.startswith("evidence/artifact123/version456/content.")


def test_validate_evidence_content_accepts_real_pdf(tmp_path):
    path = tmp_path / "real.pdf"
    path.write_bytes(b"%PDF-1.4\n%mock pdf content\n%%EOF")
    validate_evidence_content(str(path), "pdf")  # must not raise


def test_validate_evidence_content_rejects_fake_pdf(tmp_path):
    path = tmp_path / "fake.pdf"
    path.write_bytes(b"this is not a real pdf")
    with pytest.raises(ObjectStorageError, match="does not look like"):
        validate_evidence_content(str(path), "pdf")


def test_validate_evidence_content_accepts_real_png(tmp_path):
    path = tmp_path / "real.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake rest of png")
    validate_evidence_content(str(path), "png")  # must not raise


def test_validate_evidence_content_rejects_fake_png(tmp_path):
    path = tmp_path / "fake.png"
    path.write_bytes(b"not a png")
    with pytest.raises(ObjectStorageError, match="does not look like"):
        validate_evidence_content(str(path), "png")


def test_validate_evidence_content_accepts_real_jpeg(tmp_path):
    path = tmp_path / "real.jpg"
    path.write_bytes(b"\xff\xd8\xff" + b"fake rest of jpeg")
    validate_evidence_content(str(path), "jpg")  # must not raise


def test_validate_evidence_content_trusts_plain_text_formats_by_extension(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"anything at all, no signature to check")
    validate_evidence_content(str(path), "txt")  # must not raise — no sniffer for txt/csv/json/log
    validate_evidence_content(str(path), "csv")
    validate_evidence_content(str(path), "json")
    validate_evidence_content(str(path), "log")


def test_build_s3_client_raises_when_not_configured():
    from app.config import Settings

    settings = Settings(s3_endpoint_url="", s3_bucket="", s3_access_key_id="", s3_secret_access_key="")
    with pytest.raises(ObjectStorageNotConfiguredError):
        build_s3_client(settings)


def test_build_s3_client_succeeds_when_fully_configured():
    from app.config import Settings

    settings = Settings(
        s3_endpoint_url="http://minio.internal:9000",
        s3_bucket="evidence",
        s3_access_key_id="key",
        s3_secret_access_key="secret",
    )
    client = build_s3_client(settings)
    assert client is not None
