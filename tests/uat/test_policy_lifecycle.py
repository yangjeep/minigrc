"""Headless UAT: policy-version approval lifecycle (issue #31) — the
real journey draft -> submit -> approve -> effective -> superseded, run
against the real app over a real socket. See tests/uat/harness.py and
docs/superpowers/specs/2026-08-19-issue31-policy-lifecycle-design.md §7.
"""

from __future__ import annotations

import re

import pytest

from tests.uat.harness import UAT_PASSWORD, create_uat_user, extract_csrf_field, redirect_path

pytestmark = pytest.mark.uat

OPERATOR_EMAIL = "uat-policy-operator@example.com"
VALID_PDF = b"%PDF-1.4\n%mock pdf content for headless UAT\n%%EOF"

_DOWNLOAD_LINK_RE = re.compile(r"/versions/([0-9a-f]{32})/download")


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


def test_policy_version_lifecycle_draft_to_effective_to_superseded(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=OPERATOR_EMAIL, role="operator")
    client = _login(uat_client, OPERATOR_EMAIL)

    # 1. Create a policy with its first version — a real draft.
    new_page = client.get("/policies/new")
    create_response = client.post(
        "/policies",
        data={
            "title": "UAT Information Security Policy",
            "status": "draft",
            "csrf_token": extract_csrf_field(new_page.text),
        },
        files={"file": ("policy-v1.pdf", VALID_PDF, "application/pdf")},
        follow_redirects=False,
    )
    assert create_response.status_code == 303
    policy_id = redirect_path(create_response).rsplit("/", 1)[-1]

    detail_page = client.get(f"/policies/{policy_id}")
    assert detail_page.status_code == 200
    assert "badge-draft" in detail_page.text
    match = _DOWNLOAD_LINK_RE.search(detail_page.text)
    assert match is not None, "no version download link on the policy detail page"
    v1_id = match.group(1)

    # 2. Submit v1 for review.
    submit_response = client.post(
        f"/policies/{policy_id}/versions/{v1_id}/submit-for-review",
        data={"csrf_token": extract_csrf_field(detail_page.text)},
        follow_redirects=False,
    )
    assert submit_response.status_code == 303

    detail_page = client.get(f"/policies/{policy_id}")
    assert "badge-in_review" in detail_page.text

    # 3. Approve v1.
    review_response = client.post(
        f"/policies/{policy_id}/versions/{v1_id}/review",
        data={
            "decision": "approved",
            "comment": "Reviewed by UAT.",
            "csrf_token": extract_csrf_field(detail_page.text),
        },
        follow_redirects=False,
    )
    assert review_response.status_code == 303

    detail_page = client.get(f"/policies/{policy_id}")
    assert "badge-approved" in detail_page.text

    # 4. Make v1 effective.
    effective_response = client.post(
        f"/policies/{policy_id}/versions/{v1_id}/make-effective",
        data={"csrf_token": extract_csrf_field(detail_page.text)},
        follow_redirects=False,
    )
    assert effective_response.status_code == 303

    detail_page = client.get(f"/policies/{policy_id}")
    assert "badge-effective" in detail_page.text

    # 5. Upload, approve, and make effective a second version — the
    # real "a newer approved revision supersedes the old one" journey.
    upload_response = client.post(
        f"/policies/{policy_id}/versions",
        data={"change_note": "v2 revision", "csrf_token": extract_csrf_field(detail_page.text)},
        files={"file": ("policy-v2.pdf", VALID_PDF, "application/pdf")},
        follow_redirects=False,
    )
    assert upload_response.status_code == 303

    detail_page = client.get(f"/policies/{policy_id}")
    version_ids = _DOWNLOAD_LINK_RE.findall(detail_page.text)
    assert len(version_ids) == 2
    v2_id = next(v for v in version_ids if v != v1_id)

    client.post(
        f"/policies/{policy_id}/versions/{v2_id}/submit-for-review",
        data={"csrf_token": extract_csrf_field(detail_page.text)},
        follow_redirects=False,
    )
    detail_page = client.get(f"/policies/{policy_id}")
    client.post(
        f"/policies/{policy_id}/versions/{v2_id}/review",
        data={
            "decision": "approved",
            "comment": "v2 reviewed.",
            "csrf_token": extract_csrf_field(detail_page.text),
        },
        follow_redirects=False,
    )
    detail_page = client.get(f"/policies/{policy_id}")
    client.post(
        f"/policies/{policy_id}/versions/{v2_id}/make-effective",
        data={"csrf_token": extract_csrf_field(detail_page.text)},
        follow_redirects=False,
    )

    # 6. Verify the rendered page shows v2 effective, v1 superseded —
    # and that v1's own immutable file facts (never re-served here, but
    # its row must still exist and its download link still work).
    detail_page = client.get(f"/policies/{policy_id}")
    assert detail_page.status_code == 200
    assert "badge-effective" in detail_page.text
    assert "badge-superseded" in detail_page.text

    v1_download = client.get(f"/policies/{policy_id}/versions/{v1_id}/download")
    assert v1_download.status_code == 200
    assert v1_download.headers["content-type"] == "application/pdf"


def test_reader_cannot_perform_lifecycle_action(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=OPERATOR_EMAIL, role="operator")
    client = _login(uat_client, OPERATOR_EMAIL)

    new_page = client.get("/policies/new")
    create_response = client.post(
        "/policies",
        data={
            "title": "UAT Reader-Blocked Policy",
            "status": "draft",
            "csrf_token": extract_csrf_field(new_page.text),
        },
        files={"file": ("policy.pdf", VALID_PDF, "application/pdf")},
        follow_redirects=False,
    )
    policy_id = redirect_path(create_response).rsplit("/", 1)[-1]
    detail_page = client.get(f"/policies/{policy_id}")
    v1_id = _DOWNLOAD_LINK_RE.search(detail_page.text).group(1)

    reader_email = "uat-policy-reader@example.com"
    create_uat_user(session_factory, email=reader_email, role="reader")
    reader_client = _login(uat_client, reader_email)
    reader_detail_page = reader_client.get(f"/policies/{policy_id}")
    response = reader_client.post(
        f"/policies/{policy_id}/versions/{v1_id}/submit-for-review",
        data={"csrf_token": extract_csrf_field(reader_detail_page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 403
