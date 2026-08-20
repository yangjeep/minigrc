"""Browser/E2E tests for issue #45's history/point-in-time views
(app/routers/policies.py's /policies/{id}/history and
app/routers/occurrences.py's /occurrences/{id}/history) — through real
auth/CSRF/routes, not the service layer directly (that's
tests/test_history.py)."""

from __future__ import annotations

import datetime
import re

from app.control_occurrences import record_occurrence_manually
from app.models import InternalControl
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


def _submit_review_and_make_effective(client, policy_id: str, version_id: str) -> None:
    page = client.get(f"/policies/{policy_id}")
    client.post(
        f"/policies/{policy_id}/versions/{version_id}/submit-for-review",
        data={"csrf_token": extract_csrf_token(page.text)},
        follow_redirects=False,
    )
    page2 = client.get(f"/policies/{policy_id}")
    client.post(
        f"/policies/{policy_id}/versions/{version_id}/review",
        data={"decision": "approved", "comment": "ok", "csrf_token": extract_csrf_token(page2.text)},
        follow_redirects=False,
    )
    page3 = client.get(f"/policies/{policy_id}")
    client.post(
        f"/policies/{policy_id}/versions/{version_id}/make-effective",
        data={"csrf_token": extract_csrf_token(page3.text)},
        follow_redirects=False,
    )


def test_policy_history_lists_lifecycle_events(logged_in_client):
    policy_id = _create_policy_with_pdf(logged_in_client)
    version_id = _version_id(logged_in_client, policy_id)
    _submit_review_and_make_effective(logged_in_client, policy_id, version_id)

    response = logged_in_client.get(f"/policies/{policy_id}/history")
    assert response.status_code == 200
    assert "PolicyVersionSubmittedForReview" in response.text
    assert "PolicyVersionReviewed" in response.text
    assert "PolicyVersionMadeEffective" in response.text


def test_policy_history_as_of_reconstructs_past_state(logged_in_client):
    policy_id = _create_policy_with_pdf(logged_in_client)
    version_id = _version_id(logged_in_client, policy_id)
    _submit_review_and_make_effective(logged_in_client, policy_id, version_id)

    today = datetime.date.today().isoformat()
    response = logged_in_client.get(f"/policies/{policy_id}/history", params={"as_of": today})
    assert response.status_code == 200
    assert "effective" in response.text.lower()

    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    past_response = logged_in_client.get(f"/policies/{policy_id}/history", params={"as_of": yesterday})
    assert past_response.status_code == 200
    assert "No version of this policy existed yet as of this date." in past_response.text


def test_policy_history_404s_for_unknown_policy(logged_in_client):
    response = logged_in_client.get("/policies/does-not-exist/history")
    assert response.status_code == 404


def test_policy_history_rejects_malformed_as_of_gracefully(logged_in_client):
    policy_id = _create_policy_with_pdf(logged_in_client)

    response = logged_in_client.get(f"/policies/{policy_id}/history", params={"as_of": "not-a-date"})

    assert response.status_code == 200
    assert "State as of" not in response.text


def test_occurrence_history_lists_events(logged_in_client, app):
    with app.state.session_factory() as session:
        control = InternalControl(name="Access Review")
        session.add(control)
        session.flush()
        occurrence = record_occurrence_manually(session, control, actor_type="system")
        session.commit()
        occurrence_id = occurrence.id

    response = logged_in_client.get(f"/occurrences/{occurrence_id}/history")
    assert response.status_code == 200
    assert "ControlOccurrenceRecordedManually" in response.text


def test_occurrence_history_404s_for_unknown_occurrence(logged_in_client):
    response = logged_in_client.get("/occurrences/does-not-exist/history")
    assert response.status_code == 404


def test_history_view_never_mutates_state(logged_in_client, app):
    """Read-only proof at the HTTP layer: repeatedly viewing history/
    as-of reconstruction must never change DomainEvent's row count."""
    from sqlalchemy import func, select

    from app.events import DomainEvent

    policy_id = _create_policy_with_pdf(logged_in_client)
    version_id = _version_id(logged_in_client, policy_id)
    _submit_review_and_make_effective(logged_in_client, policy_id, version_id)

    with app.state.session_factory() as session:
        before = session.scalar(select(func.count()).select_from(DomainEvent))

    today = datetime.date.today().isoformat()
    logged_in_client.get(f"/policies/{policy_id}/history")
    logged_in_client.get(f"/policies/{policy_id}/history", params={"as_of": today})

    with app.state.session_factory() as session:
        after = session.scalar(select(func.count()).select_from(DomainEvent))

    assert after == before
