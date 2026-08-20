"""Route-level tests for issue #15's Digital Compliance TPM console
(/ai/tpm) and pre-fill entry points wired onto Evidence/Finding detail
pages. RBAC: any logged-in role may view; only write roles may trigger
Observe/Nag/Pre-fill actions (#37's convention).
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import AiTaskExecution, EvidenceArtifact, Finding
from tests.conftest import extract_csrf_token


def test_console_loads_for_any_logged_in_role(logged_in_client, reader_client):
    for client in (logged_in_client, reader_client):
        response = client.get("/ai/tpm")
        assert response.status_code == 200
        assert "Digital Compliance TPM" in response.text


def test_reader_cannot_trigger_observe_or_nag_scan(reader_client):
    page = reader_client.get("/ai/tpm")
    # A reader's page never renders the write-only action forms at all.
    assert 'action="/ai/tpm/observe"' not in page.text
    assert 'action="/ai/tpm/nag-scan"' not in page.text


def test_operator_can_trigger_observe(admin_client):
    page = admin_client.get("/ai/tpm")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post("/ai/tpm/observe", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert response.status_code == 303
    assert "/ai/tpm/drafts/" in response.headers["location"]


def test_admin_can_trigger_nag_scan(admin_client, app):
    with app.state.session_factory() as session:
        session.add(
            Finding(
                id="fnd-route-00001",
                title="Unowned finding",
                description="",
                severity="high",
                status="open",
                closure_note="",
            )
        )
        session.commit()

    page = admin_client.get("/ai/tpm")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post("/ai/tpm/nag-scan", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert response.status_code == 303

    with app.state.session_factory() as session:
        executions = session.scalars(select(AiTaskExecution).where(AiTaskExecution.task_type == "nag")).all()
        assert len(executions) >= 1


def test_view_draft_shows_disclaimer_and_output(admin_client):
    page = admin_client.get("/ai/tpm")
    csrf_token = extract_csrf_token(page.text)
    observe_response = admin_client.post(
        "/ai/tpm/observe", data={"csrf_token": csrf_token}, follow_redirects=True
    )
    assert observe_response.status_code == 200
    assert "not been submitted anywhere" in observe_response.text


def test_unknown_draft_id_is_404(admin_client):
    response = admin_client.get("/ai/tpm/drafts/does-not-exist")
    assert response.status_code == 404


def test_evidence_artifact_draft_description_button_and_route(admin_client, app):
    with app.state.session_factory() as session:
        artifact = EvidenceArtifact(title="Access Review Export", description="")
        session.add(artifact)
        session.commit()
        artifact_id = artifact.id

    detail_page = admin_client.get(f"/evidence-artifacts/{artifact_id}")
    assert f'action="/evidence-artifacts/{artifact_id}/ai-draft-description"' in detail_page.text
    csrf_token = extract_csrf_token(detail_page.text)

    response = admin_client.post(
        f"/evidence-artifacts/{artifact_id}/ai-draft-description",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/ai/tpm/drafts/" in response.headers["location"]

    with app.state.session_factory() as session:
        artifact_after = session.get(EvidenceArtifact, artifact_id)
        assert artifact_after.description == ""  # untouched — draft only, never auto-applied


def test_finding_draft_response_button_and_route(admin_client, app):
    with app.state.session_factory() as session:
        session.add(
            Finding(
                id="fnd-route-00002",
                title="MFA gap",
                description="",
                severity="medium",
                status="open",
                closure_note="",
            )
        )
        session.commit()

    detail_page = admin_client.get("/findings/fnd-route-00002")
    assert 'action="/findings/fnd-route-00002/ai-draft-response"' in detail_page.text
    csrf_token = extract_csrf_token(detail_page.text)

    response = admin_client.post(
        "/findings/fnd-route-00002/ai-draft-response",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        finding_after = session.get(Finding, "fnd-route-00002")
        assert finding_after.status == "open"  # untouched — draft only, never auto-applied


def test_reader_cannot_draft_finding_response(reader_client, app):
    with app.state.session_factory() as session:
        session.add(
            Finding(
                id="fnd-route-00003",
                title="Reader cannot draft",
                description="",
                severity="low",
                status="open",
                closure_note="",
            )
        )
        session.commit()

    detail_page = reader_client.get("/findings/fnd-route-00003")
    csrf_token = extract_csrf_token(detail_page.text)
    response = reader_client.post(
        "/findings/fnd-route-00003/ai-draft-response",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403
