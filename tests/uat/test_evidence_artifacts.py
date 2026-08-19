"""Headless UAT: evidence artifact repository (issue #32) — manual
capture -> download -> a second version supersedes nothing (evidence
artifacts have no lifecycle status, unlike #31's policies) but the
first version's bytes/hash remain intact -> link to a control
occurrence, over a real socket. See tests/uat/harness.py and
docs/superpowers/specs/2026-08-19-issue32-evidence-repository-design.md
§7.

Overrides tests/uat/conftest.py's `uat_server` fixture (same name) to
configure S3 (moto-backed, see design doc §7 for why) before the app is
built — the base fixture has no S3 hook to extend otherwise.
"""

from __future__ import annotations

import re

import boto3
import pytest
from moto import mock_aws

from app.config import get_settings
from tests.uat.harness import (
    UAT_PASSWORD,
    LiveServer,
    RequestRecorder,
    build_uat_app,
    create_uat_user,
    extract_csrf_field,
    redirect_path,
)

pytestmark = pytest.mark.uat

OPERATOR_EMAIL = "uat-evidence-operator@example.com"
BUCKET = "minigrc-evidence-uat"
_DOWNLOAD_LINK_RE = re.compile(r"/versions/([0-9a-f]{32})/download")


@pytest.fixture
def uat_server(tmp_path, monkeypatch):
    monkeypatch.setenv("GRC_S3_ENDPOINT_URL", "https://s3.amazonaws.com")
    monkeypatch.setenv("GRC_S3_BUCKET", BUCKET)
    monkeypatch.setenv("GRC_S3_ACCESS_KEY_ID", "fake-key")
    monkeypatch.setenv("GRC_S3_SECRET_ACCESS_KEY", "fake-secret")
    get_settings.cache_clear()

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)

        app = build_uat_app(tmp_path=tmp_path)
        with LiveServer(app) as server:
            yield server.base_url, app.state.session_factory

    get_settings.cache_clear()


@pytest.fixture
def recorder():
    return RequestRecorder()


@pytest.fixture
def uat_client(uat_server, recorder):
    import httpx

    base_url, _session_factory = uat_server
    with httpx.Client(base_url=base_url, event_hooks=recorder.event_hooks(), timeout=10.0) as client:
        yield client


def _login(client, email):
    login_page = client.get("/login")
    csrf_token = extract_csrf_field(login_page.text)
    response = client.post(
        "/login",
        data={"email": email, "password": UAT_PASSWORD, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def test_evidence_artifact_capture_download_and_link(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=OPERATOR_EMAIL, role="operator")
    client = _login(uat_client, OPERATOR_EMAIL)

    # 1. Create an evidence artifact via a real manual upload.
    new_page = client.get("/evidence-artifacts/new")
    create_response = client.post(
        "/evidence-artifacts",
        data={
            "title": "UAT Access Review Screenshot",
            "csrf_token": extract_csrf_field(new_page.text),
        },
        files={"file": ("evidence-v1.png", b"\x89PNG\r\n\x1a\nfake but sniffable png bytes", "image/png")},
        follow_redirects=False,
    )
    assert create_response.status_code == 303, create_response.headers.get("location")
    artifact_id = redirect_path(create_response).rsplit("/", 1)[-1]

    detail_page = client.get(f"/evidence-artifacts/{artifact_id}")
    assert detail_page.status_code == 200
    assert "evidence-v1.png" in detail_page.text
    match = _DOWNLOAD_LINK_RE.search(detail_page.text)
    assert match is not None
    v1_id = match.group(1)

    # 2. Download it back and verify byte-identical content.
    download_response = client.get(f"/evidence-artifacts/{artifact_id}/versions/{v1_id}/download")
    assert download_response.status_code == 200
    assert download_response.content == b"\x89PNG\r\n\x1a\nfake but sniffable png bytes"

    # 3. Capture a second version — first version's bytes must survive.
    upload_response = client.post(
        f"/evidence-artifacts/{artifact_id}/versions",
        data={"csrf_token": extract_csrf_field(detail_page.text)},
        files={"file": ("evidence-v2.png", b"\x89PNG\r\n\x1a\ndifferent fake png bytes", "image/png")},
        follow_redirects=False,
    )
    assert upload_response.status_code == 303, upload_response.headers.get("location")

    detail_page_after = client.get(f"/evidence-artifacts/{artifact_id}")
    version_ids = _DOWNLOAD_LINK_RE.findall(detail_page_after.text)
    assert len(version_ids) == 2
    v2_id = next(v for v in version_ids if v != v1_id)

    v1_download_after = client.get(f"/evidence-artifacts/{artifact_id}/versions/{v1_id}/download")
    assert v1_download_after.content == b"\x89PNG\r\n\x1a\nfake but sniffable png bytes"
    v2_download = client.get(f"/evidence-artifacts/{artifact_id}/versions/{v2_id}/download")
    assert v2_download.content == b"\x89PNG\r\n\x1a\ndifferent fake png bytes"

    # 4. Link the second version to a real control occurrence.
    create_control_response = client.post(
        "/api/registers/controls",
        json={"name": "UAT evidence-linked control"},
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
    )
    assert create_control_response.status_code == 201, create_control_response.text
    control_id = create_control_response.json()["id"]

    control_page = client.get(f"/controls/{control_id}")
    generate_response = client.post(
        f"/controls/{control_id}/occurrences",
        data={"due_on": "", "csrf_token": extract_csrf_field(control_page.text)},
        follow_redirects=False,
    )
    assert generate_response.status_code == 303
    # create_control_occurrence redirects back to the control page, not
    # the occurrence — find the real occurrence id from the control
    # page's own rendered link instead.
    control_page_after = client.get(f"/controls/{control_id}")
    occurrence_match = re.search(r"/occurrences/([0-9a-f]{32})", control_page_after.text)
    assert occurrence_match is not None
    occurrence_id = occurrence_match.group(1)

    detail_page_final = client.get(f"/evidence-artifacts/{artifact_id}")
    link_response = client.post(
        f"/evidence-artifacts/{artifact_id}/versions/{v2_id}/link-occurrence",
        data={"occurrence_id": occurrence_id, "csrf_token": extract_csrf_field(detail_page_final.text)},
        follow_redirects=False,
    )
    assert link_response.status_code == 303
    assert "flash_kind=error" not in link_response.headers["location"]


def test_reader_cannot_capture_evidence(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=OPERATOR_EMAIL, role="operator")
    operator_client = _login(uat_client, OPERATOR_EMAIL)

    new_page = operator_client.get("/evidence-artifacts/new")
    create_response = operator_client.post(
        "/evidence-artifacts",
        data={"title": "Reader-blocked artifact", "csrf_token": extract_csrf_field(new_page.text)},
        files={"file": ("e.txt", b"content", "text/plain")},
        follow_redirects=False,
    )
    artifact_id = redirect_path(create_response).rsplit("/", 1)[-1]

    reader_email = "uat-evidence-reader@example.com"
    create_uat_user(session_factory, email=reader_email, role="reader")
    reader_client = _login(uat_client, reader_email)
    detail_page = reader_client.get(f"/evidence-artifacts/{artifact_id}")
    response = reader_client.post(
        f"/evidence-artifacts/{artifact_id}/versions",
        data={"csrf_token": extract_csrf_field(detail_page.text)},
        files={"file": ("blocked.txt", b"should never be captured", "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 403
