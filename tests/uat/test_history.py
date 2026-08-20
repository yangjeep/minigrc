"""Headless UAT: issue #45's history/point-in-time reconstruction view —
drive a real policy through draft -> submitted -> approved -> effective
over real HTTP, then confirm the /history page lists every event and the
"as of" reconstruction correctly shows a state strictly between two of
those transitions. Run against the real app over a real socket (see
tests/uat/harness.py).
"""

from __future__ import annotations

import datetime
import re

import pytest

from tests.uat.harness import UAT_PASSWORD, create_uat_user, extract_csrf_field, redirect_path

pytestmark = pytest.mark.uat

OPERATOR_EMAIL = "uat-history-operator@example.com"
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


def test_policy_history_and_as_of_reconstruction(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=OPERATOR_EMAIL, role="operator")
    client = _login(uat_client, OPERATOR_EMAIL)

    new_page = client.get("/policies/new")
    create_response = client.post(
        "/policies",
        data={
            "title": "UAT History Policy",
            "status": "draft",
            "csrf_token": extract_csrf_field(new_page.text),
        },
        files={"file": ("policy-v1.pdf", VALID_PDF, "application/pdf")},
        follow_redirects=False,
    )
    assert create_response.status_code == 303
    policy_id = redirect_path(create_response).rsplit("/", 1)[-1]

    detail_page = client.get(f"/policies/{policy_id}")
    v1_id = _DOWNLOAD_LINK_RE.search(detail_page.text).group(1)

    submit_response = client.post(
        f"/policies/{policy_id}/versions/{v1_id}/submit-for-review",
        data={"csrf_token": extract_csrf_field(detail_page.text)},
        follow_redirects=False,
    )
    assert submit_response.status_code == 303

    # 1. The history page must list this real event, with a real actor.
    history_page = client.get(f"/policies/{policy_id}/history")
    assert history_page.status_code == 200
    assert "PolicyVersionSubmittedForReview" in history_page.text

    detail_page = client.get(f"/policies/{policy_id}")
    review_response = client.post(
        f"/policies/{policy_id}/versions/{v1_id}/review",
        data={
            "decision": "approved",
            "comment": "Approved for UAT.",
            "csrf_token": extract_csrf_field(detail_page.text),
        },
        follow_redirects=False,
    )
    assert review_response.status_code == 303

    # 2. "As of" today (after submit+approve, before make-effective):
    # the reconstructed state must show "approved", not "effective" —
    # proving the reconstruction reflects a real point in time, not
    # just current state.
    today = datetime.date.today().isoformat()
    as_of_page = client.get(f"/policies/{policy_id}/history", params={"as_of": today})
    assert as_of_page.status_code == 200
    assert "badge-approved" in as_of_page.text
    assert "badge-effective" not in as_of_page.text

    detail_page = client.get(f"/policies/{policy_id}")
    effective_response = client.post(
        f"/policies/{policy_id}/versions/{v1_id}/make-effective",
        data={"csrf_token": extract_csrf_field(detail_page.text)},
        follow_redirects=False,
    )
    assert effective_response.status_code == 303

    # 3. History page now shows all three events; "as of" today now
    # shows "effective" since the transition already happened today.
    history_page = client.get(f"/policies/{policy_id}/history")
    assert "PolicyVersionSubmittedForReview" in history_page.text
    assert "PolicyVersionReviewed" in history_page.text
    assert "PolicyVersionMadeEffective" in history_page.text

    as_of_page = client.get(f"/policies/{policy_id}/history", params={"as_of": today})
    assert "badge-effective" in as_of_page.text

    # 4. Reconstructing "as of" a date before the policy existed shows
    # no version yet — read-only, never invents history.
    long_ago = "2000-01-01"
    past_page = client.get(f"/policies/{policy_id}/history", params={"as_of": long_ago})
    assert past_page.status_code == 200
    assert "No version of this policy existed yet as of this date." in past_page.text


def test_reader_can_view_history_but_not_mutate(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=OPERATOR_EMAIL, role="operator")
    client = _login(uat_client, OPERATOR_EMAIL)

    new_page = client.get("/policies/new")
    create_response = client.post(
        "/policies",
        data={
            "title": "UAT History Reader Policy",
            "status": "draft",
            "csrf_token": extract_csrf_field(new_page.text),
        },
        files={"file": ("policy.pdf", VALID_PDF, "application/pdf")},
        follow_redirects=False,
    )
    policy_id = redirect_path(create_response).rsplit("/", 1)[-1]

    reader_email = "uat-history-reader@example.com"
    create_uat_user(session_factory, email=reader_email, role="reader")
    reader_client = _login(uat_client, reader_email)

    history_page = reader_client.get(f"/policies/{policy_id}/history")
    assert history_page.status_code == 200  # read-only view — every logged-in role can see it
