"""Headless UAT: the audit/PBC package export (issue #34) — build a
representative period (a control, an effective policy version, a
performed occurrence, a control test with an exception, evidence, and a
finding with a remediation update), generate the package over a real
socket, and independently re-verify the manifest's hashes against the
embedded file bytes. See tests/uat/harness.py and
docs/superpowers/specs/2026-08-19-issue34-audit-pbc-package-design.md §5.

Overrides tests/uat/conftest.py's `uat_server` fixture to configure S3
(moto-backed) before the app is built — mirrors
tests/uat/test_evidence_artifacts.py's identical override.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile

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
    csrf_header_value,
    extract_csrf_field,
    redirect_path,
)

pytestmark = pytest.mark.uat

ADMIN_EMAIL = "uat-package-admin@example.com"
BUCKET = "minigrc-audit-package-uat"
VALID_PDF = b"%PDF-1.4\n%mock pdf content for headless UAT\n%%EOF"
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


def test_generated_package_contains_a_verifiable_manifest_of_a_representative_period(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=ADMIN_EMAIL, role="admin")
    client = _login(uat_client, ADMIN_EMAIL)

    # 1. Define scope with an audit period.
    scope_form = client.get("/onboarding/scope")
    define_response = client.post(
        "/onboarding/scope",
        data={
            "service_description": "UAT service",
            "audit_period_starts_on": "2026-01-01",
            "audit_period_ends_on": "2026-12-31",
            "csrf_token": extract_csrf_field(scope_form.text),
        },
        follow_redirects=False,
    )
    assert define_response.status_code == 303

    # 2. A control and a tester.
    create_control = client.post(
        "/api/registers/controls",
        json={
            "name": "UAT Package Control",
            "owner": "security@example.com",
            "status": "not_started",
            "review_frequency": "annual",
            "description": "UAT-created control for the package export scenario.",
        },
        headers={"X-CSRF-Token": csrf_header_value(client)},
    )
    assert create_control.status_code == 201, create_control.text
    control_id = create_control.json()["id"]

    person_page = client.get("/people/new")
    create_person = client.post(
        "/people",
        data={"email": "uat-package-tester@example.com", "csrf_token": extract_csrf_field(person_page.text)},
        follow_redirects=False,
    )
    assert create_person.status_code == 303
    person_id = redirect_path(create_person).rsplit("/", 1)[-1]

    # 3. An occurrence, performed within the period.
    occurrence_page = client.get(f"/controls/{control_id}")
    create_occurrence = client.post(
        f"/controls/{control_id}/occurrences",
        data={"due_on": "2026-02-01", "csrf_token": extract_csrf_field(occurrence_page.text)},
        follow_redirects=False,
    )
    assert create_occurrence.status_code == 303
    control_detail = client.get(f"/controls/{control_id}")
    occurrence_match = re.search(r'/occurrences/([0-9a-f]{32})"', control_detail.text)
    assert occurrence_match is not None
    occurrence_id = occurrence_match.group(1)
    occurrence_detail = client.get(f"/occurrences/{occurrence_id}")
    perform_response = client.post(
        f"/occurrences/{occurrence_id}/perform",
        data={
            "performed_on": "2026-02-02",
            "scope_note": "Reviewed the access list",
            "csrf_token": extract_csrf_field(occurrence_detail.text),
        },
        follow_redirects=False,
    )
    assert perform_response.status_code == 303

    # 4. A policy, approved and made effective within the period.
    policy_new = client.get("/policies/new")
    create_policy = client.post(
        "/policies",
        data={
            "title": "UAT Security Policy",
            "status": "draft",
            "csrf_token": extract_csrf_field(policy_new.text),
        },
        files={"file": ("policy.pdf", VALID_PDF, "application/pdf")},
        follow_redirects=False,
    )
    assert create_policy.status_code == 303
    policy_id = redirect_path(create_policy).rsplit("/", 1)[-1]
    policy_detail = client.get(f"/policies/{policy_id}")
    version_id = _DOWNLOAD_LINK_RE.search(policy_detail.text).group(1)

    client.post(
        f"/policies/{policy_id}/versions/{version_id}/submit-for-review",
        data={"csrf_token": extract_csrf_field(policy_detail.text)},
        follow_redirects=False,
    )
    policy_detail = client.get(f"/policies/{policy_id}")
    client.post(
        f"/policies/{policy_id}/versions/{version_id}/review",
        data={
            "decision": "approved",
            "comment": "Looks good.",
            "csrf_token": extract_csrf_field(policy_detail.text),
        },
        follow_redirects=False,
    )
    policy_detail = client.get(f"/policies/{policy_id}")
    client.post(
        f"/policies/{policy_id}/versions/{version_id}/make-effective",
        data={"csrf_token": extract_csrf_field(policy_detail.text)},
        follow_redirects=False,
    )

    # 5. A control test with an exception, within the period.
    test_form = client.get("/control-tests/new")
    create_test = client.post(
        "/control-tests",
        data={
            "control_id": control_id,
            "procedure_description": "Inspected the access list",
            "tester_person_id": person_id,
            "performed_on": "2026-02-15",
            "result": "exceptions_noted",
            "exception_lines": "One stale credential found",
            "csrf_token": extract_csrf_field(test_form.text),
        },
        follow_redirects=False,
    )
    assert create_test.status_code == 303
    test_id = redirect_path(create_test).rsplit("/", 1)[-1]

    # 6. A finding tied to that test, with a remediation update.
    finding_form = client.get(f"/findings/new?source_test_id={test_id}")
    create_finding = client.post(
        "/findings",
        data={
            "title": "Stale credential found during testing",
            "severity": "medium",
            "control_id": control_id,
            "source_test_id": test_id,
            "csrf_token": extract_csrf_field(finding_form.text),
        },
        follow_redirects=False,
    )
    assert create_finding.status_code == 303
    finding_id = redirect_path(create_finding).rsplit("/", 1)[-1]
    finding_detail = client.get(f"/findings/{finding_id}")
    client.post(
        f"/findings/{finding_id}/remediation-updates",
        data={"note": "Revoked the credential.", "csrf_token": extract_csrf_field(finding_detail.text)},
        follow_redirects=False,
    )

    # 7. Generate the package and independently re-verify its manifest.
    package_page = client.get("/admin/audit-package")
    generate_response = client.post(
        "/admin/audit-package",
        data={"csrf_token": extract_csrf_field(package_page.text)},
        follow_redirects=False,
    )
    assert generate_response.status_code == 200
    assert generate_response.headers["content-type"] == "application/zip"

    with zipfile.ZipFile(io.BytesIO(generate_response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["disclaimer"]
        assert manifest["audit_period_starts_on"] == "2026-01-01"
        for entry in manifest["files"]:
            content = archive.read(entry["path"])
            assert hashlib.sha256(content).hexdigest() == entry["sha256"]

        occurrences = json.loads(archive.read("occurrences.json"))
        assert occurrence_id in {o["id"] for o in occurrences}

        control_tests = json.loads(archive.read("control_tests.json"))
        matching_test = next(t for t in control_tests if t["id"] == test_id)
        assert matching_test["exceptions"][0]["description"] == "One stale credential found"

        findings = json.loads(archive.read("findings.json"))
        matching_finding = next(f for f in findings if f["id"] == finding_id)
        assert matching_finding["remediation_updates"][0]["note"] == "Revoked the credential."

        policies = json.loads(archive.read("policies.json"))
        matching_policy = next(p for p in policies if p["id"] == policy_id)
        assert matching_policy["versions"][0]["lifecycle_status"] == "effective"
        embedded_policy_path = f"policies/{policy_id}/version-1-policy.pdf"
        assert embedded_policy_path in archive.namelist()


def test_reader_cannot_access_the_package_route(uat_server, uat_client):
    _base_url, session_factory = uat_server
    reader_email = "uat-package-reader@example.com"
    create_uat_user(session_factory, email=reader_email, role="reader")
    client = _login(uat_client, reader_email)

    response = client.get("/admin/audit-package", follow_redirects=False)
    assert response.status_code == 403
