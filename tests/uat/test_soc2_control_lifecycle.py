"""Headless UAT: SOC 2-primary, ISO-preserved control operations lifecycle
(issue #38's required scenarios 1-10), run against the real app over a
real socket — see tests/uat/harness.py and
docs/superpowers/specs/2026-08-18-issue38-headless-uat-design.md §6.

Every identifier used below is sourced from a real HTTP response — a
redirect Location, a rendered page's link, or the same register-grid JSON
API the browser's own JS calls — never from a side-channel query against
`app.state.session_factory` issued from this test's own thread. Building
this surfaced a real, pre-existing read-after-write correctness gap,
mostly filed and fixed as issue #55 (`app/db.py::_invalidate_on_checkin`)
with a rarer, backend-independent residual tracked as issue #57 — sourcing
every id from a real HTTP response was always the design intent here for
UAT faithfulness, and incidentally avoids racing either gap far more
often than a raw DB peek would. One exception remains at the very end
(the evidence-occurrence link has no rendered surface anywhere in the
app) and uses `tests/uat/harness.py::poll_for_visibility`, kept as
defense-in-depth.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from app.aws_connector import AwsCheckResult, build_evidence_snapshot
from app.models import ControlOccurrenceEvidence
from tests.uat.harness import (
    UAT_PASSWORD,
    create_uat_user,
    csrf_header_value,
    extract_csrf_field,
    poll_for_visibility,
    redirect_path,
)

pytestmark = pytest.mark.uat

ADMIN_EMAIL = "uat-admin@example.com"
OWNER_EMAIL = "uat-owner@example.com"

_OCCURRENCE_LINK_RE = re.compile(r"/occurrences/([0-9a-f]{32})")


def _first_requirement_id(client, framework_id: str) -> str:
    """The real register-grid JSON API a framework's requirements grid
    calls on page load (see frameworks/detail.html's RegisterGrid.init) —
    not a side-channel DB read."""
    response = client.get(f"/api/registers/framework-requirements?framework_id={framework_id}")
    assert response.status_code == 200, response.text
    requirements = response.json()
    assert requirements, f"framework {framework_id} has no requirements"
    return requirements[0]["id"]


@pytest.mark.parametrize("uat_server", ["sqlite", "postgres"], indirect=True)
def test_soc2_control_operations_lifecycle(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=ADMIN_EMAIL, role="admin")

    # 1. Login.
    login_page = uat_client.get("/login")
    assert login_page.status_code == 200
    csrf_token = extract_csrf_field(login_page.text)
    login_response = uat_client.post(
        "/login",
        data={"email": ADMIN_EMAIL, "password": UAT_PASSWORD, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert login_response.status_code == 303
    assert uat_client.cookies.get("session")

    # 2/3. SOC 2 is primary and renders first; ISO 27001 remains available.
    frameworks_page = uat_client.get("/frameworks")
    assert frameworks_page.status_code == 200
    soc2_index = frameworks_page.text.index("SOC 2 Trust Services Criteria")
    iso_index = frameworks_page.text.index("ISO/IEC 27001")
    assert soc2_index < iso_index, "SOC 2 must render before ISO 27001 (primary-first ordering)"
    assert 'badge-primary">Primary' in frameworks_page.text

    soc2_href = re.search(r'href="(/frameworks/[0-9a-f]{32})"[^>]*>SOC 2', frameworks_page.text)
    iso_href = re.search(r'href="(/frameworks/[0-9a-f]{32})"[^>]*>ISO/IEC 27001', frameworks_page.text)
    assert soc2_href and iso_href, "could not locate SOC 2 / ISO framework links on the frameworks list"
    soc2_id = soc2_href.group(1).rsplit("/", 1)[-1]
    iso_id = iso_href.group(1).rsplit("/", 1)[-1]

    iso_detail = uat_client.get(f"/frameworks/{iso_id}")
    assert iso_detail.status_code == 200
    assert "Requirements" in iso_detail.text

    soc2_requirement_id = _first_requirement_id(uat_client, soc2_id)
    iso_requirement_id = _first_requirement_id(uat_client, iso_id)

    # 3. Create a control with cadence, mapped to both SOC 2 and ISO —
    # via the real register-grid JSON API (X-CSRF-Token), the same path
    # the browser's JS uses.
    create_control_response = uat_client.post(
        "/api/registers/controls",
        json={
            "name": "UAT access review",
            "status": "in_progress",
            "review_frequency": "quarterly",
            "cadence_type": "calendar",
            "cadence_interval_months": 1,
        },
        headers={"X-CSRF-Token": csrf_header_value(uat_client)},
    )
    assert create_control_response.status_code == 201, create_control_response.text
    control_id = create_control_response.json()["id"]

    for requirement_id in (soc2_requirement_id, iso_requirement_id):
        control_page = uat_client.get(f"/controls/{control_id}")
        map_response = uat_client.post(
            f"/controls/{control_id}/mappings",
            data={"requirement_id": requirement_id, "csrf_token": extract_csrf_field(control_page.text)},
            follow_redirects=False,
        )
        assert map_response.status_code == 303

    # 4. Owner — person id sourced from the real create-person redirect.
    person_form = uat_client.get("/people/new")
    person_response = uat_client.post(
        "/people",
        data={
            "email": OWNER_EMAIL,
            "display_name": "UAT Owner",
            "csrf_token": extract_csrf_field(person_form.text),
        },
        follow_redirects=False,
    )
    assert person_response.status_code == 303
    owner_person_id = redirect_path(person_response).rsplit("/", 1)[-1]

    control_page = uat_client.get(f"/controls/{control_id}")
    owner_response = uat_client.post(
        f"/controls/{control_id}/owner",
        data={"owner_person_id": owner_person_id, "csrf_token": extract_csrf_field(control_page.text)},
        follow_redirects=False,
    )
    assert owner_response.status_code == 303

    # 5. Create and activate a control period.
    periods_page = uat_client.get("/control-periods")
    create_period_response = uat_client.post(
        "/control-periods",
        data={
            "name": "UAT operating period",
            "starts_on": "2026-01-01",
            "ends_on": "2026-12-31",
            "csrf_token": extract_csrf_field(periods_page.text),
        },
        follow_redirects=False,
    )
    assert create_period_response.status_code == 303
    period_id = redirect_path(create_period_response).rsplit("/", 1)[-1]

    period_page = uat_client.get(f"/control-periods/{period_id}")
    activate_response = uat_client.post(
        f"/control-periods/{period_id}/activate",
        data={"csrf_token": extract_csrf_field(period_page.text)},
        follow_redirects=False,
    )
    assert activate_response.status_code == 303

    # 6. Generate the expected occurrence for the active period, then read
    # the occurrence id back off the control's own rendered page — the
    # same link a real user would click "Record performance" from.
    period_page = uat_client.get(f"/control-periods/{period_id}")
    generate_response = uat_client.post(
        f"/control-periods/{period_id}/generate-occurrences",
        data={"csrf_token": extract_csrf_field(period_page.text)},
        follow_redirects=False,
    )
    assert generate_response.status_code == 303

    control_page_after_generate = uat_client.get(f"/controls/{control_id}")
    assert control_page_after_generate.status_code == 200
    occurrence_links = _OCCURRENCE_LINK_RE.findall(control_page_after_generate.text)
    assert occurrence_links, "generate-occurrences did not render any occurrence for this control"
    occurrence_id = occurrence_links[0]

    # 8 (seeded ahead of 7, since perform links evidence in one submit):
    # a deterministic FAKE AWS check result, run through the real
    # build_evidence_snapshot the CLI/UI connector path uses — no network
    # call, no real AWS credentials. This one direct session write (not a
    # read racing a server-side write) is the harness's sanctioned
    # "deterministic fake external provider" construction.
    fake_result = AwsCheckResult(
        check_key="cloudtrail_posture",
        status="pass",
        title="UAT CloudTrail posture (synthetic)",
        summary="Synthetic evidence generated by the headless UAT harness — not a real AWS collection.",
        normalized_payload={"trails": 1, "multi_region": True},
    )
    with session_factory() as session:
        evidence = build_evidence_snapshot(fake_result, connection_id="uat-fake-aws-connection")
        session.add(evidence)
        session.commit()
        session.refresh(evidence)
        evidence_id = evidence.id

    # The occurrence-detail form only lists NOT-yet-linked evidence, so
    # confirm our synthetic snapshot is actually offered before submitting
    # against it — a real user would see the same dropdown.
    occurrence_page = uat_client.get(f"/occurrences/{occurrence_id}")
    assert occurrence_page.status_code == 200
    assert f'value="{evidence_id}"' in occurrence_page.text

    # 7. Perform the occurrence, linking the evidence in the same submit
    # (the real occurrence-detail form's evidence_snapshot_id field).
    perform_response = uat_client.post(
        f"/occurrences/{occurrence_id}/perform",
        data={
            "performed_on": "2026-01-15",
            "performed_by_person_id": owner_person_id,
            "scope_note": "Headless UAT performance claim.",
            "evidence_note": "Synthetic evidence attached by the headless UAT harness.",
            "evidence_snapshot_id": evidence_id,
            "csrf_token": extract_csrf_field(occurrence_page.text),
        },
        follow_redirects=False,
    )
    assert perform_response.status_code == 303

    # 9. Verify rendered/current occurrence state changed correctly.
    occurrence_detail = uat_client.get(f"/occurrences/{occurrence_id}")
    assert occurrence_detail.status_code == 200
    assert "Headless UAT performance claim." in occurrence_detail.text
    assert "performed on 2026-01-15" in occurrence_detail.text
    assert "UAT Owner" in occurrence_detail.text

    control_detail_after = uat_client.get(f"/controls/{control_id}")
    assert control_detail_after.status_code == 200
    assert "No completed occurrences yet." not in control_detail_after.text

    # The evidence-occurrence link has no rendered surface anywhere in the
    # app today (occurrences/detail.html shows the performance claim but
    # not which evidence snapshot got linked) — a direct read is the only
    # way to observe it. poll_for_visibility retries: a bare read here
    # measurably misses a just-committed write often enough to flake (see
    # the confirmed app/db.py SQLite read-after-write finding tracked
    # separately — this is a real, pre-existing app behavior, not
    # something an explicit commit() on the read side papers over).
    links = poll_for_visibility(
        session_factory,
        lambda session: session.scalars(
            select(ControlOccurrenceEvidence).where(ControlOccurrenceEvidence.occurrence_id == occurrence_id)
        ).all(),
    )
    assert [link.evidence_snapshot_id for link in links] == [evidence_id]

    # 10. The same InternalControl remains mapped across SOC 2 and ISO —
    # one control, two framework mappings, no duplicated operational state
    # (PRD §4.1's shared-control invariant) — via the control's own
    # rendered "Mapped requirements" table, not a DB peek.
    mapped_section = control_detail_after.text.split("Mapped requirements", 1)[1]
    assert "SOC 2 Trust Services Criteria" in mapped_section
    assert "ISO/IEC 27001" in mapped_section
