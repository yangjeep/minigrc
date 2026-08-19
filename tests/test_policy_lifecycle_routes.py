"""Route-level tests for issue #31's policy-version lifecycle actions:
submit-for-review, review, make-effective, withdraw, control-mappings,
and the retire_policy regression. Domain-function unit tests live in
tests/test_policy_lifecycle.py.
"""

from __future__ import annotations

import re

from app.models import InternalControl, Policy, PolicyVersion
from tests.conftest import extract_csrf_token

VALID_PDF = b"%PDF-1.4\n%mock pdf content for tests\n%%EOF"
_DOWNLOAD_LINK_RE = re.compile(r"/versions/([0-9a-f]{32})/download")


def _create_policy_with_pdf(client, *, title="Security Policy") -> str:
    page = client.get("/policies/new")
    csrf_token = extract_csrf_token(page.text)
    files = {"file": ("policy.pdf", VALID_PDF, "application/pdf")}
    response = client.post(
        "/policies",
        data={"title": title, "status": "draft", "csrf_token": csrf_token},
        files=files,
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].split("?")[0].rsplit("/", 1)[-1]


def _version_id(client, policy_id: str) -> str:
    detail = client.get(f"/policies/{policy_id}")
    match = _DOWNLOAD_LINK_RE.search(detail.text)
    assert match is not None
    return match.group(1)


def test_submit_for_review_route(logged_in_client, app):
    policy_id = _create_policy_with_pdf(logged_in_client)
    version_id = _version_id(logged_in_client, policy_id)

    page = logged_in_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/policies/{policy_id}/versions/{version_id}/submit-for-review",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        version = session.get(PolicyVersion, version_id)
        assert version.lifecycle_status == "in_review"


def test_submit_for_review_twice_fails_cleanly(logged_in_client, app):
    policy_id = _create_policy_with_pdf(logged_in_client)
    version_id = _version_id(logged_in_client, policy_id)
    page = logged_in_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    logged_in_client.post(
        f"/policies/{policy_id}/versions/{version_id}/submit-for-review",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )

    page2 = logged_in_client.get(f"/policies/{policy_id}")
    csrf_token2 = extract_csrf_token(page2.text)
    response = logged_in_client.post(
        f"/policies/{policy_id}/versions/{version_id}/submit-for-review",
        data={"csrf_token": csrf_token2},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        version = session.get(PolicyVersion, version_id)
        assert version.lifecycle_status == "in_review"  # unchanged, not corrupted


def _submit_and_approve(client, policy_id: str, version_id: str) -> None:
    page = client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    client.post(
        f"/policies/{policy_id}/versions/{version_id}/submit-for-review",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    page2 = client.get(f"/policies/{policy_id}")
    csrf_token2 = extract_csrf_token(page2.text)
    response = client.post(
        f"/policies/{policy_id}/versions/{version_id}/review",
        data={"decision": "approved", "comment": "ok", "csrf_token": csrf_token2},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_review_approve_route(logged_in_client, app):
    policy_id = _create_policy_with_pdf(logged_in_client)
    version_id = _version_id(logged_in_client, policy_id)
    _submit_and_approve(logged_in_client, policy_id, version_id)

    with app.state.session_factory() as session:
        version = session.get(PolicyVersion, version_id)
        assert version.lifecycle_status == "approved"


def test_review_reject_requires_comment(logged_in_client, app):
    policy_id = _create_policy_with_pdf(logged_in_client)
    version_id = _version_id(logged_in_client, policy_id)
    page = logged_in_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    logged_in_client.post(
        f"/policies/{policy_id}/versions/{version_id}/submit-for-review",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )

    page2 = logged_in_client.get(f"/policies/{policy_id}")
    csrf_token2 = extract_csrf_token(page2.text)
    response = logged_in_client.post(
        f"/policies/{policy_id}/versions/{version_id}/review",
        data={"decision": "rejected", "comment": "", "csrf_token": csrf_token2},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        version = session.get(PolicyVersion, version_id)
        assert version.lifecycle_status == "in_review"  # rejection was rejected, no change


def test_make_effective_route(logged_in_client, app):
    policy_id = _create_policy_with_pdf(logged_in_client)
    version_id = _version_id(logged_in_client, policy_id)
    _submit_and_approve(logged_in_client, policy_id, version_id)

    page = logged_in_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/policies/{policy_id}/versions/{version_id}/make-effective",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        version = session.get(PolicyVersion, version_id)
        assert version.lifecycle_status == "effective"


def test_make_effective_supersedes_previous_via_routes(logged_in_client, app):
    policy_id = _create_policy_with_pdf(logged_in_client)
    v1_id = _version_id(logged_in_client, policy_id)
    _submit_and_approve(logged_in_client, policy_id, v1_id)
    page = logged_in_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    logged_in_client.post(
        f"/policies/{policy_id}/versions/{v1_id}/make-effective",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )

    # Upload a second version through the real upload route.
    page2 = logged_in_client.get(f"/policies/{policy_id}")
    csrf_token2 = extract_csrf_token(page2.text)
    logged_in_client.post(
        f"/policies/{policy_id}/versions",
        data={"change_note": "v2", "csrf_token": csrf_token2},
        files={"file": ("policy-v2.pdf", VALID_PDF, "application/pdf")},
        follow_redirects=False,
    )
    with app.state.session_factory() as session:
        policy = session.get(Policy, policy_id)
        v2 = next(v for v in policy.versions if v.id != v1_id)
        v2_id = v2.id

    _submit_and_approve(logged_in_client, policy_id, v2_id)
    page3 = logged_in_client.get(f"/policies/{policy_id}")
    csrf_token3 = extract_csrf_token(page3.text)
    logged_in_client.post(
        f"/policies/{policy_id}/versions/{v2_id}/make-effective",
        data={"csrf_token": csrf_token3},
        follow_redirects=False,
    )

    with app.state.session_factory() as session:
        v1 = session.get(PolicyVersion, v1_id)
        v2 = session.get(PolicyVersion, v2_id)
        assert v1.lifecycle_status == "superseded"
        assert v1.superseded_by_version_id == v2_id
        assert v2.lifecycle_status == "effective"


def test_withdraw_route(logged_in_client, app):
    policy_id = _create_policy_with_pdf(logged_in_client)
    version_id = _version_id(logged_in_client, policy_id)

    page = logged_in_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/policies/{policy_id}/versions/{version_id}/withdraw",
        data={"reason": "no longer needed", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        version = session.get(PolicyVersion, version_id)
        assert version.lifecycle_status == "withdrawn"
        assert version.withdrawn_reason == "no longer needed"


def test_retire_policy_withdraws_effective_version(logged_in_client, app):
    """Regression: retiring a policy must not leave a version claiming
    to be the current effective document (issue #31 §5)."""
    policy_id = _create_policy_with_pdf(logged_in_client)
    version_id = _version_id(logged_in_client, policy_id)
    _submit_and_approve(logged_in_client, policy_id, version_id)
    page = logged_in_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    logged_in_client.post(
        f"/policies/{policy_id}/versions/{version_id}/make-effective",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )

    page2 = logged_in_client.get(f"/policies/{policy_id}")
    csrf_token2 = extract_csrf_token(page2.text)
    response = logged_in_client.post(
        f"/policies/{policy_id}/retire",
        data={"csrf_token": csrf_token2},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        policy = session.get(Policy, policy_id)
        version = session.get(PolicyVersion, version_id)
        assert policy.status == "retired"
        assert version.lifecycle_status == "withdrawn"
        assert version.withdrawn_reason == "Policy retired"


def test_retire_policy_with_no_effective_version_is_unaffected(logged_in_client, app):
    """Regression guard the other direction: retiring a policy with no
    effective version must not error or fabricate a withdrawal."""
    policy_id = _create_policy_with_pdf(logged_in_client)
    version_id = _version_id(logged_in_client, policy_id)

    page = logged_in_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/policies/{policy_id}/retire",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        policy = session.get(Policy, policy_id)
        version = session.get(PolicyVersion, version_id)
        assert policy.status == "retired"
        assert version.lifecycle_status == "draft"  # untouched


def test_add_control_mapping_route(logged_in_client, app):
    policy_id = _create_policy_with_pdf(logged_in_client)
    with app.state.session_factory() as session:
        control = InternalControl(name="Access control review")
        session.add(control)
        session.commit()
        control_id = control.id

    page = logged_in_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/policies/{policy_id}/control-mappings",
        data={"control_id": control_id, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        policy = session.get(Policy, policy_id)
        assert [m.control_id for m in policy.control_mappings] == [control_id]


def test_add_control_mapping_rejects_duplicate(logged_in_client, app):
    policy_id = _create_policy_with_pdf(logged_in_client)
    with app.state.session_factory() as session:
        control = InternalControl(name="Access control review")
        session.add(control)
        session.commit()
        control_id = control.id

    page = logged_in_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    logged_in_client.post(
        f"/policies/{policy_id}/control-mappings",
        data={"control_id": control_id, "csrf_token": csrf_token},
        follow_redirects=False,
    )

    page2 = logged_in_client.get(f"/policies/{policy_id}")
    csrf_token2 = extract_csrf_token(page2.text)
    response = logged_in_client.post(
        f"/policies/{policy_id}/control-mappings",
        data={"control_id": control_id, "csrf_token": csrf_token2},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        policy = session.get(Policy, policy_id)
        assert len(policy.control_mappings) == 1


# --- RBAC regression (issue #37's require_write_access pattern) ----------


def test_lifecycle_routes_require_write_access(reader_client, auditor_client, logged_in_client, app):
    policy_id = _create_policy_with_pdf(logged_in_client)
    version_id = _version_id(logged_in_client, policy_id)

    for client in (reader_client, auditor_client):
        page = client.get(f"/policies/{policy_id}")
        csrf_token = extract_csrf_token(page.text)
        response = client.post(
            f"/policies/{policy_id}/versions/{version_id}/submit-for-review",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert response.status_code == 403

    with app.state.session_factory() as session:
        version = session.get(PolicyVersion, version_id)
        assert version.lifecycle_status == "draft"


def test_control_mapping_route_requires_write_access(reader_client, logged_in_client, app):
    policy_id = _create_policy_with_pdf(logged_in_client)
    with app.state.session_factory() as session:
        control = InternalControl(name="Access control review")
        session.add(control)
        session.commit()
        control_id = control.id

    page = reader_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    response = reader_client.post(
        f"/policies/{policy_id}/control-mappings",
        data={"control_id": control_id, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        policy = session.get(Policy, policy_id)
        assert policy.control_mappings == []
