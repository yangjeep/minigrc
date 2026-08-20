"""Tests for issue #47's PII redaction module (app/pii_redaction.py).

Covers both redaction targets: Person directory-entry PII (never
embedded in any DomainEvent payload, so redacting the plain row is
sufficient) and User login-email PII frozen verbatim into ten plain
String columns across the schema (_ACTOR_STRING_COLUMNS).
"""

from __future__ import annotations

import datetime

from sqlalchemy import select

from app.control_occurrences import generate_occurrences, record_occurrence_manually
from app.models import (
    AuditEvent,
    ControlPeriod,
    ControlTestPopulation,
    ControlTestSample,
    ExternalConnection,
    Framework,
    FrameworkRequirement,
    ImportJob,
    InternalControl,
    Job,
    Person,
    PolicyVersion,
    RequirementAssessment,
    Secret,
    User,
)
from app.pii_redaction import redact_person_pii, redact_user_pii
from app.security import hash_password

NOW = datetime.datetime(2026, 1, 1)


def _make_actor(session) -> User:
    actor = User(email="pii-admin@example.com", password_hash=hash_password("x"), role="admin")
    session.add(actor)
    session.flush()
    return actor


# --- redact_person_pii -------------------------------------------------------


def test_redact_person_pii_rejects_non_departed(app):
    with app.state.session_factory() as session:
        actor = _make_actor(session)
        person = Person(email="active@example.com", display_name="Active Person", employment_status="active")
        session.add(person)
        session.flush()

        try:
            redact_person_pii(session, person, actor=actor)
            raised = False
        except ValueError:
            raised = True
        assert raised
        assert person.email == "active@example.com"  # unchanged


def test_redact_person_pii_redacts_departed_person(app):
    with app.state.session_factory() as session:
        actor = _make_actor(session)
        person = Person(
            email="departed@example.com",
            display_name="Departed Person",
            employment_status="departed",
            external_id="hris-1234",
        )
        session.add(person)
        session.flush()
        person_id = person.id

        report = redact_person_pii(session, person, actor=actor)
        session.commit()

        assert report.already_redacted is False
        session.refresh(person)
        assert person.id == person_id  # identity reference stays stable
        assert person.email == f"redacted-person-{person_id}@redacted.invalid"
        assert person.display_name == "Redacted Person"
        assert person.external_id is None
        assert person.pii_redacted_at is not None

        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.entity_type == "person", AuditEvent.action == "redact_pii")
        )
        assert audit is not None
        assert audit.actor == actor.email


def test_redact_person_pii_is_idempotent(app):
    with app.state.session_factory() as session:
        actor = _make_actor(session)
        person = Person(email="departed@example.com", employment_status="departed")
        session.add(person)
        session.flush()

        redact_person_pii(session, person, actor=actor)
        session.commit()
        redacted_email = person.email

        report = redact_person_pii(session, person, actor=actor)
        session.commit()

        assert report.already_redacted is True
        assert person.email == redacted_email  # unchanged by the second call


def test_redacted_person_still_resolves_via_control_occurrence(app):
    """The exact scenario the issue's own execution prompt names: a
    departed person's PII is redacted while historical
    control-occurrence attribution (responsible_person_id) remains
    intact and resolvable."""
    with app.state.session_factory() as session:
        actor = _make_actor(session)
        person = Person(email="owner@example.com", display_name="Owner", employment_status="active")
        session.add(person)
        session.flush()

        control = InternalControl(
            name="Access Review",
            cadence_type="calendar",
            cadence_interval_months=3,
            owner_person_id=person.id,
        )
        session.add(control)
        session.flush()

        period = ControlPeriod(
            name="H1",
            starts_on=datetime.date(2026, 1, 1),
            ends_on=datetime.date(2026, 6, 30),
            created_by=actor.email,
            status="active",
        )
        session.add(period)
        session.flush()

        occurrences = generate_occurrences(session, control, period=period, actor_type="system")
        session.commit()
        occurrence_id = occurrences[0].id
        assert occurrences[0].responsible_person_id == person.id

        person.employment_status = "departed"
        session.flush()
        redact_person_pii(session, person, actor=actor)
        session.commit()

        from app.models import ControlOccurrence

        occurrence = session.get(ControlOccurrence, occurrence_id)
        assert occurrence.responsible_person_id == person.id  # reference untouched
        resolved_person = session.get(Person, occurrence.responsible_person_id)
        assert resolved_person.display_name == "Redacted Person"  # now shows redacted display


# --- redact_user_pii ----------------------------------------------------------


def test_redact_user_pii_rejects_active_user(app):
    with app.state.session_factory() as session:
        actor = _make_actor(session)
        user = User(email="active-user@example.com", password_hash=hash_password("x"), status="active")
        session.add(user)
        session.flush()

        try:
            redact_user_pii(session, user, actor=actor)
            raised = False
        except ValueError:
            raised = True
        assert raised


def test_redact_user_pii_is_idempotent(app):
    with app.state.session_factory() as session:
        actor = _make_actor(session)
        user = User(email="disabled@example.com", password_hash=hash_password("x"), status="disabled")
        session.add(user)
        session.flush()

        redact_user_pii(session, user, actor=actor)
        session.commit()
        redacted_email = user.email

        report = redact_user_pii(session, user, actor=actor)
        session.commit()

        assert report.already_redacted is True
        assert user.email == redacted_email


def test_redact_user_pii_sweeps_every_actor_string_column(app):
    with app.state.session_factory() as session:
        actor = _make_actor(session)
        departed_email = "departed-user@example.com"
        user = User(email=departed_email, password_hash=hash_password("x"), status="disabled")
        session.add(user)
        session.flush()
        user_id = user.id

        # AuditEvent
        from app.audit import record_audit_event

        record_audit_event(
            session,
            entity_type="control",
            entity_id="c1",
            action="update",
            detail="did something",
            actor=departed_email,
        )
        # PolicyVersion
        from app.models import Policy

        policy = Policy(title="Test Policy")
        session.add(policy)
        session.flush()
        session.add(
            PolicyVersion(
                policy_id=policy.id,
                version_number=1,
                original_filename="p.pdf",
                stored_filename="s.pdf",
                media_type="application/pdf",
                byte_size=1,
                sha256="a" * 64,
                uploader=departed_email,
            )
        )
        # ControlPeriod
        period = ControlPeriod(
            name="Period",
            starts_on=datetime.date(2026, 1, 1),
            ends_on=datetime.date(2026, 6, 30),
            created_by=departed_email,
        )
        session.add(period)
        # ControlTestPopulation / ControlTestSample
        control = InternalControl(name="Control")
        session.add(control)
        session.flush()
        population = ControlTestPopulation(control_id=control.id, created_by=departed_email)
        session.add(population)
        session.flush()
        session.add(ControlTestSample(population_id=population.id, created_by=departed_email))
        # RequirementAssessment
        framework = Framework(name="F", version="1")
        session.add(framework)
        session.flush()
        requirement = FrameworkRequirement(framework_id=framework.id, reference_code="R1", title="Req 1")
        session.add(requirement)
        session.flush()
        session.add(RequirementAssessment(requirement_id=requirement.id, last_reviewed_by=departed_email))
        # Secret
        session.add(Secret(name="s1", kind="env_ref", env_var_name="SOME_VAR", created_by=departed_email))
        # ExternalConnection
        session.add(
            ExternalConnection(
                name="conn1", db_type="sqlite", sqlite_path="/tmp/x.db", created_by=departed_email
            )
        )
        # Job
        session.add(Job(job_type="test_job", created_by=departed_email))
        # ImportJob
        session.add(ImportJob(source="cli", importer_name="test_importer", created_by=departed_email))
        session.commit()

        report = redact_user_pii(session, user, actor=actor)
        session.commit()

        assert report.already_redacted is False
        expected_tables = {
            "audit_events",
            "policy_versions",
            "control_periods",
            "control_test_populations",
            "control_test_samples",
            "requirement_assessments",
            "secrets",
            "external_connections",
            "jobs",
            "import_jobs",
        }
        assert set(report.rows_updated_by_table) == expected_tables
        assert all(count == 1 for count in report.rows_updated_by_table.values())

        new_email = f"redacted-user-{user_id}@redacted.invalid"
        session.refresh(user)
        assert user.email == new_email
        assert user.pii_redacted_at is not None

        # Every swept row now shows the pseudonym, not the real email.
        assert session.scalar(select(AuditEvent).where(AuditEvent.actor == departed_email)) is None
        assert (
            session.scalar(
                select(AuditEvent).where(AuditEvent.actor == new_email, AuditEvent.action == "update")
            )
            is not None
        )
        assert session.scalar(select(PolicyVersion).where(PolicyVersion.uploader == new_email)) is not None
        assert session.scalar(select(ControlPeriod).where(ControlPeriod.created_by == new_email)) is not None
        assert (
            session.scalar(select(ControlTestPopulation).where(ControlTestPopulation.created_by == new_email))
            is not None
        )
        assert (
            session.scalar(select(ControlTestSample).where(ControlTestSample.created_by == new_email))
            is not None
        )
        assert (
            session.scalar(
                select(RequirementAssessment).where(RequirementAssessment.last_reviewed_by == new_email)
            )
            is not None
        )
        assert session.scalar(select(Secret).where(Secret.created_by == new_email)) is not None
        assert (
            session.scalar(select(ExternalConnection).where(ExternalConnection.created_by == new_email))
            is not None
        )
        assert session.scalar(select(Job).where(Job.created_by == new_email)) is not None
        assert session.scalar(select(ImportJob).where(ImportJob.created_by == new_email)) is not None


def test_redact_user_pii_never_touches_unrelated_actor_strings(app):
    """A different user's rows must not be swept just because the
    redacted user's rows share a table."""
    with app.state.session_factory() as session:
        actor = _make_actor(session)
        departed = User(email="departed@example.com", password_hash=hash_password("x"), status="disabled")
        unrelated_email = "still-active@example.com"
        session.add(departed)
        session.flush()

        from app.audit import record_audit_event

        record_audit_event(
            session,
            entity_type="control",
            entity_id="c1",
            action="update",
            detail="unrelated",
            actor=unrelated_email,
        )
        session.commit()

        redact_user_pii(session, departed, actor=actor)
        session.commit()

        assert session.scalar(select(AuditEvent).where(AuditEvent.actor == unrelated_email)) is not None


def test_record_occurrence_manually_still_resolves_after_redaction(app):
    """Manual occurrence recording (the other ControlOccurrence
    creation path) also keeps resolving through a redacted person's
    stable id."""
    with app.state.session_factory() as session:
        actor = _make_actor(session)
        person = Person(email="manual-owner@example.com", employment_status="active")
        session.add(person)
        session.flush()
        control = InternalControl(name="Manual Control", owner_person_id=person.id)
        session.add(control)
        session.flush()

        occurrence = record_occurrence_manually(session, control, actor_type="system")
        session.commit()

        person.employment_status = "departed"
        session.flush()
        redact_person_pii(session, person, actor=actor)
        session.commit()

        session.refresh(occurrence)
        assert occurrence.control_id == control.id  # unaffected by the redaction
