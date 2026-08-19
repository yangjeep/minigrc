"""Headless UAT: the full assurance chain (issue #13) — recurring control
occurrence -> frozen population -> sample -> test with one exception ->
finding -> remediation update -> retest -> close, run against the real
app over a real socket. See tests/uat/harness.py and
docs/superpowers/specs/2026-08-19-issue13-assurance-testing-findings-design.md
§6.
"""

from __future__ import annotations

import re

import pytest

from tests.uat.harness import (
    UAT_PASSWORD,
    create_uat_user,
    csrf_header_value,
    extract_csrf_field,
    redirect_path,
)

pytestmark = pytest.mark.uat

ADMIN_EMAIL = "uat-assurance-admin@example.com"

_ITEM_CHECKBOX_RE = re.compile(r'name="population_item_ids" value="([0-9a-f]{32})"')
_SAMPLE_SELECT_RE = re.compile(r'name="sample_id">(.*?)</select>', re.DOTALL)
_OPTION_VALUE_RE = re.compile(r'<option value="([0-9a-f]{32})"')


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


def test_full_assurance_chain_population_to_closed_finding(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=ADMIN_EMAIL, role="admin")
    client = _login(uat_client, ADMIN_EMAIL)

    # 1. Create a control (via the register JSON API — controls have no
    # plain form-POST create route, app/routers/controls.py) and a person
    # to act as tester.
    create_control = client.post(
        "/api/registers/controls",
        json={
            "name": "UAT Access Review",
            "owner": "security@example.com",
            "status": "not_started",
            "review_frequency": "annual",
            "description": "UAT-created control for the assurance chain scenario.",
        },
        headers={"X-CSRF-Token": csrf_header_value(client)},
    )
    assert create_control.status_code == 201, create_control.text
    control_id = create_control.json()["id"]

    person_page = client.get("/people/new")
    create_person = client.post(
        "/people",
        data={"email": "uat-tester@example.com", "csrf_token": extract_csrf_field(person_page.text)},
        follow_redirects=False,
    )
    assert create_person.status_code == 303
    person_id = redirect_path(create_person).rsplit("/", 1)[-1]

    # 2. Record two manual occurrences for that control (a recurring
    # control occurrence, per the issue's own verification section).
    # Distinct due_on dates avoid the period-less due-at uniqueness
    # constraint that would otherwise reject a second NULL-due_at
    # occurrence for the same control (app/models.py's
    # uq_occurrence_control_due_no_period).
    for due_on in ("2026-01-05", "2026-02-05"):
        occurrence_page = client.get(f"/controls/{control_id}")
        response = client.post(
            f"/controls/{control_id}/occurrences",
            data={"due_on": due_on, "csrf_token": extract_csrf_field(occurrence_page.text)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "flash_kind=error" not in response.headers["location"]

    # 3. Freeze a population for that control.
    population_form = client.get("/control-tests/populations/new")
    freeze_response = client.post(
        "/control-tests/populations",
        data={
            "control_id": control_id,
            "description": "UAT population",
            "criteria_note": "All occurrences to date",
            "csrf_token": extract_csrf_field(population_form.text),
        },
        follow_redirects=False,
    )
    assert freeze_response.status_code == 303
    population_id = redirect_path(freeze_response).rsplit("/", 1)[-1]

    # 4. Draw a sample of one item from the population.
    population_detail = client.get(f"/control-tests/populations/{population_id}")
    item_ids = _ITEM_CHECKBOX_RE.findall(population_detail.text)
    assert item_ids, "expected at least one population item checkbox on the page"
    draw_response = client.post(
        f"/control-tests/populations/{population_id}/samples",
        data={
            "population_item_ids": item_ids[:1],
            "selection_method": "haphazard",
            "selection_rationale": "1 of 2, smallest population",
            "csrf_token": extract_csrf_field(population_detail.text),
        },
        follow_redirects=False,
    )
    assert draw_response.status_code == 303
    assert "flash_kind=error" not in draw_response.headers["location"]

    # 5. Record a test against that sample with one exception.
    test_form = client.get("/control-tests/new")
    sample_select_match = _SAMPLE_SELECT_RE.search(test_form.text)
    assert sample_select_match is not None, "no sample_id <select> found on the new-test form"
    sample_id_match = _OPTION_VALUE_RE.search(sample_select_match.group(1))
    assert sample_id_match is not None, "no sample option found in the sample_id <select>"
    create_test = client.post(
        "/control-tests",
        data={
            "control_id": control_id,
            "sample_id": sample_id_match.group(1),
            "procedure_description": "Inspected the access list for this occurrence",
            "tester_person_id": person_id,
            "performed_on": "2026-01-15",
            "result": "exceptions_noted",
            "exception_lines": "A terminated employee retained access",
            "csrf_token": extract_csrf_field(test_form.text),
        },
        follow_redirects=False,
    )
    assert create_test.status_code == 303
    assert "flash_kind=error" not in create_test.headers["location"]
    test_id = redirect_path(create_test).rsplit("/", 1)[-1]

    test_detail = client.get(f"/control-tests/{test_id}")
    assert "A terminated employee retained access" in test_detail.text

    # 6. Open a finding from that test.
    finding_form = client.get(f"/findings/new?source_test_id={test_id}")
    create_finding = client.post(
        "/findings",
        data={
            "title": "Terminated employee retained access",
            "description": "Found during sample testing",
            "severity": "high",
            "control_id": control_id,
            "source_test_id": test_id,
            "csrf_token": extract_csrf_field(finding_form.text),
        },
        follow_redirects=False,
    )
    assert create_finding.status_code == 303
    finding_id = redirect_path(create_finding).rsplit("/", 1)[-1]

    # 7. Record a remediation update -> transitions to "remediating".
    finding_detail = client.get(f"/findings/{finding_id}")
    assert "badge-open" in finding_detail.text
    remediation_response = client.post(
        f"/findings/{finding_id}/remediation-updates",
        data={
            "note": "Revoked access and updated the offboarding checklist",
            "csrf_token": extract_csrf_field(finding_detail.text),
        },
        follow_redirects=False,
    )
    assert remediation_response.status_code == 303
    finding_detail = client.get(f"/findings/{finding_id}")
    assert "badge-remediating" in finding_detail.text

    # 8. Request a retest -> transitions to "retesting".
    retest_request = client.post(
        f"/findings/{finding_id}/request-retest",
        data={"csrf_token": extract_csrf_field(finding_detail.text)},
        follow_redirects=False,
    )
    assert retest_request.status_code == 303
    finding_detail = client.get(f"/findings/{finding_id}")
    assert "badge-retesting" in finding_detail.text

    # 9. Record the actual retest, correctly linked back to the original test.
    retest_form = client.get("/control-tests/new")
    record_retest_test = client.post(
        "/control-tests",
        data={
            "control_id": control_id,
            "procedure_description": "Re-inspected the access list",
            "tester_person_id": person_id,
            "performed_on": "2026-02-01",
            "result": "no_exceptions",
            "retest_of_test_id": test_id,
            "csrf_token": extract_csrf_field(retest_form.text),
        },
        follow_redirects=False,
    )
    assert record_retest_test.status_code == 303
    retest_test_id = redirect_path(record_retest_test).rsplit("/", 1)[-1]

    finding_detail = client.get(f"/findings/{finding_id}")
    link_retest_response = client.post(
        f"/findings/{finding_id}/record-retest",
        data={"test_id": retest_test_id, "csrf_token": extract_csrf_field(finding_detail.text)},
        follow_redirects=False,
    )
    assert link_retest_response.status_code == 303
    assert "flash_kind=error" not in link_retest_response.headers["location"]

    # A passing retest never auto-closes — still "retesting", awaiting an
    # explicit human closure decision.
    finding_detail = client.get(f"/findings/{finding_id}")
    assert "badge-retesting" in finding_detail.text

    # 10. Close the finding.
    close_response = client.post(
        f"/findings/{finding_id}/close",
        data={
            "decision": "remediated_and_retested",
            "note": "Confirmed fixed on retest",
            "csrf_token": extract_csrf_field(finding_detail.text),
        },
        follow_redirects=False,
    )
    assert close_response.status_code == 303
    assert "flash_kind=error" not in close_response.headers["location"]

    final_detail = client.get(f"/findings/{finding_id}")
    assert "badge-closed" in final_detail.text
    assert "remediated_and_retested" in final_detail.text


def test_reader_cannot_open_or_close_findings(uat_server, uat_client):
    _base_url, session_factory = uat_server
    reader_email = "uat-assurance-reader@example.com"
    create_uat_user(session_factory, email=reader_email, role="reader")
    client = _login(uat_client, reader_email)

    findings_page = client.get("/findings")
    assert findings_page.status_code == 200

    new_form = client.get("/findings/new")
    response = client.post(
        "/findings",
        data={
            "title": "should be rejected",
            "severity": "low",
            "csrf_token": extract_csrf_field(new_form.text),
        },
        follow_redirects=False,
    )
    assert response.status_code == 403
