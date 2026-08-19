"""Route-level tests for issue #32's evidence artifact repository —
create/upload/download/link-occurrence over real HTTP, RBAC boundaries
(#37), and the "not configured" failure mode. Domain-function tests
live in tests/test_evidence_repository.py.

Overrides the `app` fixture (same name as tests/conftest.py's) so every
fixture that depends on it (client/logged_in_client/admin_client/
reader_client/auditor_client) automatically gets an S3-configured app
for this file only — see design doc §7 for why moto, not a live
service container.
"""

from __future__ import annotations

import re

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import select

from app.config import get_settings
from app.main import create_app
from app.models import EvidenceArtifact, InternalControl
from tests.conftest import extract_csrf_token

BUCKET = "minigrc-evidence-test"
_DOWNLOAD_LINK_RE = re.compile(r"/versions/([0-9a-f]{32})/download")


@pytest.fixture
def app(tmp_path, monkeypatch):
    # moto only intercepts requests to AWS's own recognized service
    # endpoints — an arbitrary self-hosted-style endpoint_url (e.g. a
    # real MinIO URL) bypasses its HTTP interception entirely and would
    # attempt a real network connection. This is purely a moto testing
    # limitation, not a product one: app/object_storage.py::build_s3_client
    # accepts any endpoint_url in production (see
    # test_object_storage.py::test_build_s3_client_succeeds_when_fully_configured,
    # which builds a real client against a custom endpoint with no
    # network call at construction time).
    monkeypatch.setenv("GRC_S3_ENDPOINT_URL", "https://s3.amazonaws.com")
    monkeypatch.setenv("GRC_S3_BUCKET", BUCKET)
    monkeypatch.setenv("GRC_S3_ACCESS_KEY_ID", "fake-key")
    monkeypatch.setenv("GRC_S3_SECRET_ACCESS_KEY", "fake-secret")
    get_settings.cache_clear()

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        db_path = str(tmp_path / "test.db")
        yield create_app(database_path=db_path)

    get_settings.cache_clear()


def _create_artifact(client, *, title="Server Access Log Screenshot"):
    page = client.get("/evidence-artifacts/new")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        "/evidence-artifacts",
        data={"title": title, "csrf_token": csrf_token},
        files={"file": ("evidence.txt", b"evidence content", "text/plain")},
        follow_redirects=False,
    )
    location = response.headers.get("location", "")
    assert response.status_code == 303, location
    assert "flash_kind=error" not in location, location
    return location.split("?")[0].rsplit("/", 1)[-1]


def test_create_evidence_artifact_and_download_round_trip(logged_in_client, app):
    artifact_id = _create_artifact(logged_in_client)

    detail = logged_in_client.get(f"/evidence-artifacts/{artifact_id}")
    assert detail.status_code == 200
    assert "evidence.txt" in detail.text
    match = _DOWNLOAD_LINK_RE.search(detail.text)
    assert match is not None
    version_id = match.group(1)

    download = logged_in_client.get(f"/evidence-artifacts/{artifact_id}/versions/{version_id}/download")
    assert download.status_code == 200
    assert download.content == b"evidence content"
    # Starlette's Response appends "; charset=utf-8" for text media
    # types by default — expected framework behavior, not this route's
    # own choice.
    assert download.headers["content-type"].startswith("text/plain")
    assert "evidence.txt" in download.headers["content-disposition"]
    assert download.headers["x-content-type-options"] == "nosniff"


def test_upload_new_version(logged_in_client, app):
    artifact_id = _create_artifact(logged_in_client)

    page = logged_in_client.get(f"/evidence-artifacts/{artifact_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/evidence-artifacts/{artifact_id}/versions",
        data={"csrf_token": csrf_token},
        files={"file": ("evidence-v2.txt", b"different content", "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        artifact = session.get(EvidenceArtifact, artifact_id)
        assert len(artifact.versions) == 2
        assert {v.version_number for v in artifact.versions} == {1, 2}


def test_duplicate_upload_does_not_create_a_second_version(logged_in_client, app):
    artifact_id = _create_artifact(logged_in_client)

    page = logged_in_client.get(f"/evidence-artifacts/{artifact_id}")
    csrf_token = extract_csrf_token(page.text)
    logged_in_client.post(
        f"/evidence-artifacts/{artifact_id}/versions",
        data={"csrf_token": csrf_token},
        files={"file": ("evidence.txt", b"evidence content", "text/plain")},  # same bytes as the first
        follow_redirects=False,
    )

    with app.state.session_factory() as session:
        artifact = session.get(EvidenceArtifact, artifact_id)
        assert len(artifact.versions) == 1


def test_unsupported_file_type_rejected(logged_in_client, app):
    page = logged_in_client.get("/evidence-artifacts/new")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/evidence-artifacts",
        data={"title": "Bad file", "csrf_token": csrf_token},
        files={"file": ("malware.exe", b"MZ\x90\x00fake exe", "application/octet-stream")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        artifact = session.scalar(select(EvidenceArtifact).where(EvidenceArtifact.title == "Bad file"))
        assert artifact is None


def test_spoofed_pdf_extension_rejected(logged_in_client, app):
    page = logged_in_client.get("/evidence-artifacts/new")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/evidence-artifacts",
        data={"title": "Spoofed", "csrf_token": csrf_token},
        files={"file": ("fake.pdf", b"not actually a pdf", "application/pdf")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        artifact = session.scalar(select(EvidenceArtifact).where(EvidenceArtifact.title == "Spoofed"))
        assert artifact is None


def test_link_evidence_version_to_control_occurrence(logged_in_client, app):
    artifact_id = _create_artifact(logged_in_client)
    with app.state.session_factory() as session:
        artifact = session.get(EvidenceArtifact, artifact_id)
        version_id = artifact.latest_version.id

        control = InternalControl(name="Access review")
        session.add(control)
        session.commit()

    from app.control_occurrences import record_occurrence_manually

    with app.state.session_factory() as session:
        control = session.scalar(select(InternalControl).where(InternalControl.name == "Access review"))
        occurrence = record_occurrence_manually(session, control, actor_type="user")
        session.commit()
        occurrence_id = occurrence.id

    page = logged_in_client.get(f"/evidence-artifacts/{artifact_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/evidence-artifacts/{artifact_id}/versions/{version_id}/link-occurrence",
        data={"occurrence_id": occurrence_id, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]

    with app.state.session_factory() as session:
        from app.models import ControlOccurrenceEvidenceArtifact

        link = session.scalar(
            select(ControlOccurrenceEvidenceArtifact).where(
                ControlOccurrenceEvidenceArtifact.occurrence_id == occurrence_id
            )
        )
        assert link is not None
        assert link.evidence_artifact_version_id == version_id


def test_duplicate_link_rejected(logged_in_client, app):
    artifact_id = _create_artifact(logged_in_client)
    with app.state.session_factory() as session:
        artifact = session.get(EvidenceArtifact, artifact_id)
        version_id = artifact.latest_version.id
        control = InternalControl(name="Access review")
        session.add(control)
        session.commit()

    from app.control_occurrences import record_occurrence_manually

    with app.state.session_factory() as session:
        control = session.scalar(select(InternalControl).where(InternalControl.name == "Access review"))
        occurrence = record_occurrence_manually(session, control, actor_type="user")
        session.commit()
        occurrence_id = occurrence.id

    for _ in range(2):
        page = logged_in_client.get(f"/evidence-artifacts/{artifact_id}")
        csrf_token = extract_csrf_token(page.text)
        response = logged_in_client.post(
            f"/evidence-artifacts/{artifact_id}/versions/{version_id}/link-occurrence",
            data={"occurrence_id": occurrence_id, "csrf_token": csrf_token},
            follow_redirects=False,
        )
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        from app.models import ControlOccurrenceEvidenceArtifact

        links = session.scalars(
            select(ControlOccurrenceEvidenceArtifact).where(
                ControlOccurrenceEvidenceArtifact.occurrence_id == occurrence_id
            )
        ).all()
        assert len(links) == 1


# --- RBAC (issue #37's require_write_access pattern) ----------------------


def test_create_requires_write_access(reader_client, app):
    page = reader_client.get("/evidence-artifacts/new")
    csrf_token = extract_csrf_token(page.text)
    response = reader_client.post(
        "/evidence-artifacts",
        data={"title": "Reader attempt", "csrf_token": csrf_token},
        files={"file": ("evidence.txt", b"content", "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        artifact = session.scalar(select(EvidenceArtifact).where(EvidenceArtifact.title == "Reader attempt"))
        assert artifact is None


def test_upload_version_requires_write_access(reader_client, logged_in_client, app):
    artifact_id = _create_artifact(logged_in_client)

    page = reader_client.get(f"/evidence-artifacts/{artifact_id}")
    csrf_token = extract_csrf_token(page.text)
    response = reader_client.post(
        f"/evidence-artifacts/{artifact_id}/versions",
        data={"csrf_token": csrf_token},
        files={"file": ("v2.txt", b"reader should not add this", "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        artifact = session.get(EvidenceArtifact, artifact_id)
        assert len(artifact.versions) == 1


def test_reader_can_still_download(reader_client, logged_in_client, app):
    """Read access to evidence stays available to reader/auditor — only
    mutation is write-gated."""
    artifact_id = _create_artifact(logged_in_client)
    with app.state.session_factory() as session:
        artifact = session.get(EvidenceArtifact, artifact_id)
        version_id = artifact.latest_version.id

    response = reader_client.get(f"/evidence-artifacts/{artifact_id}/versions/{version_id}/download")
    assert response.status_code == 200
    assert response.content == b"evidence content"


# --- Not-configured failure mode -------------------------------------------


def test_new_artifact_form_fails_cleanly_when_not_configured(logged_in_client, app):
    # app.state.settings is a snapshot built once in create_app — mutating
    # env vars post-creation has no effect on it, so mutate it directly,
    # same pattern tests/test_google_oidc.py's _enable_google_oidc uses.
    app.state.settings.s3_endpoint_url = ""
    response = logged_in_client.get("/evidence-artifacts/new")
    assert response.status_code == 503
