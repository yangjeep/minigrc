"""Domain-level tests for issue #34's audit/PBC package export
(app/audit_package.py). Route-level tests live in
tests/test_audit_package_routes.py.

Most tests use the standard `app` fixture (no evidence repository
configured) since they only exercise scope/period/policy logic. Tests
that need to embed EvidenceArtifactVersion bytes use the separate
`s3_app` fixture below, mirroring
tests/test_evidence_artifacts_routes.py's S3-configured app override —
kept as its own fixture name (not overriding the shared `app` fixture)
so both flavors coexist in one file.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import os
import zipfile

import boto3
import pytest
from moto import mock_aws

from app.audit_package import AuditPackageError, generate_audit_package
from app.compliance_scope import define_or_revise_scope
from app.config import get_settings
from app.control_occurrences import (
    link_evidence_artifact_version,
    perform_occurrence,
    record_occurrence_manually,
)
from app.control_tests import record_test
from app.finding_lifecycle import close_finding, open_finding
from app.main import create_app
from app.models import (
    EvidenceArtifact,
    EvidenceArtifactVersion,
    InternalControl,
    Person,
    Policy,
    PolicyVersion,
)
from app.object_storage import build_s3_client, capture_raw_bytes, object_key_for, upload_object
from app.policy_lifecycle import make_version_effective, review_version, submit_for_review
from app.storage import _save_policy_version, policy_version_path

BUCKET = "minigrc-audit-package-test"


@pytest.fixture
def s3_app(tmp_path, monkeypatch):
    monkeypatch.setenv("GRC_S3_ENDPOINT_URL", "https://s3.amazonaws.com")
    monkeypatch.setenv("GRC_S3_BUCKET", BUCKET)
    monkeypatch.setenv("GRC_S3_ACCESS_KEY_ID", "fake-key")
    monkeypatch.setenv("GRC_S3_SECRET_ACCESS_KEY", "fake-secret")
    get_settings.cache_clear()

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        db_path = str(tmp_path / "test.db")
        yield create_app(database_path=db_path)

    get_settings.cache_clear()


def _make_control(session, **kwargs):
    defaults = {"name": "Test control", "owner": "owner@example.com"}
    defaults.update(kwargs)
    control = InternalControl(**defaults)
    session.add(control)
    session.flush()
    return control


def _define_scope(session, starts_on, ends_on):
    define_or_revise_scope(
        session,
        service_description="UAT service",
        audit_period_starts_on=starts_on,
        audit_period_ends_on=ends_on,
    )
    session.commit()


def test_no_scope_defined_raises(app):
    with app.state.session_factory() as session:
        with pytest.raises(AuditPackageError):
            generate_audit_package(session, app.state.settings, actor_email="admin@example.com")


def test_scope_with_no_period_dates_raises(app):
    with app.state.session_factory() as session:
        define_or_revise_scope(session, service_description="x")
        session.commit()

        with pytest.raises(AuditPackageError):
            generate_audit_package(session, app.state.settings, actor_email="admin@example.com")


def _zip_json(package_bytes: bytes, path: str):
    with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
        return json.loads(archive.read(path))


def test_occurrence_in_period_included_out_of_period_excluded(app):
    with app.state.session_factory() as session:
        _define_scope(session, datetime.date(2026, 1, 1), datetime.date(2026, 3, 31))
        control = _make_control(session)
        session.commit()

        in_period = record_occurrence_manually(
            session, control, due_at=datetime.datetime(2026, 2, 1, 12, 0, 0)
        )
        out_of_period = record_occurrence_manually(
            session, control, due_at=datetime.datetime(2026, 6, 1, 12, 0, 0)
        )
        session.commit()

        package_bytes, _hash = generate_audit_package(
            session, app.state.settings, actor_email="admin@example.com"
        )
        occurrences = _zip_json(package_bytes, "occurrences.json")
        occurrence_ids = {o["id"] for o in occurrences}
        assert in_period.id in occurrence_ids
        assert out_of_period.id not in occurrence_ids


def test_control_test_in_period_included_out_of_period_excluded(app):
    with app.state.session_factory() as session:
        _define_scope(session, datetime.date(2026, 1, 1), datetime.date(2026, 3, 31))
        control = _make_control(session)
        person = Person(email="tester@example.com")
        session.add(person)
        session.commit()

        in_period_test = record_test(
            session,
            control,
            procedure_description="x",
            tester_person_id=person.id,
            performed_at=datetime.datetime(2026, 2, 1),
            result="no_exceptions",
        )
        out_of_period_test = record_test(
            session,
            control,
            procedure_description="x",
            tester_person_id=person.id,
            performed_at=datetime.datetime(2026, 6, 1),
            result="no_exceptions",
        )
        session.commit()

        package_bytes, _hash = generate_audit_package(
            session, app.state.settings, actor_email="admin@example.com"
        )
        tests = _zip_json(package_bytes, "control_tests.json")
        test_ids = {t["id"] for t in tests}
        assert in_period_test.id in test_ids
        assert out_of_period_test.id not in test_ids


def test_finding_still_open_at_period_end_is_included_even_if_opened_earlier(app):
    with app.state.session_factory() as session:
        _define_scope(session, datetime.date(2026, 3, 1), datetime.date(2026, 3, 31))
        finding = open_finding(session, title="Old but still open", severity="low")
        session.commit()

        package_bytes, _hash = generate_audit_package(
            session, app.state.settings, actor_email="admin@example.com"
        )
        findings = _zip_json(package_bytes, "findings.json")
        assert finding.id in {f["id"] for f in findings}


def test_closed_finding_created_before_period_is_excluded(app):
    with app.state.session_factory() as session:
        finding = open_finding(session, title="x", severity="low")
        close_finding(session, finding, decision="false_positive")
        session.commit()

        _define_scope(session, datetime.date(2027, 1, 1), datetime.date(2027, 12, 31))

        package_bytes, _hash = generate_audit_package(
            session, app.state.settings, actor_email="admin@example.com"
        )
        findings = _zip_json(package_bytes, "findings.json")
        assert finding.id not in {f["id"] for f in findings}


def _make_effective_policy_version(session, data_dir, *, title, content: bytes):
    policy = Policy(title=title)
    session.add(policy)
    session.flush()
    stored = _save_policy_version(
        iter([content]),
        original_filename="policy.pdf",
        extension="pdf",
        data_dir=data_dir,
        policy_id=policy.id,
        version_number=1,
        max_bytes=1024 * 1024,
    )
    version = PolicyVersion(
        policy_id=policy.id,
        version_number=1,
        original_filename="policy.pdf",
        stored_filename=stored.stored_filename,
        media_type="application/pdf",
        byte_size=stored.byte_size,
        sha256=stored.sha256,
        uploader="uploader@example.com",
    )
    session.add(version)
    session.flush()
    submit_for_review(session, version, actor_id="reviewer-1")
    review_version(session, version, decision="approved", actor_id="reviewer-1")
    make_version_effective(session, version)
    session.commit()
    return policy, version


def test_effective_policy_version_is_embedded_with_matching_hash(app):
    with app.state.session_factory() as session:
        _define_scope(session, datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))
        policy, version = _make_effective_policy_version(
            session, app.state.settings.data_dir, title="Security Policy", content=b"%PDF-1.4 policy bytes"
        )

        package_bytes, _hash = generate_audit_package(
            session, app.state.settings, actor_email="admin@example.com"
        )
        policies = _zip_json(package_bytes, "policies.json")
        assert len(policies) == 1
        assert policies[0]["id"] == policy.id
        assert policies[0]["versions"][0]["sha256"] == version.sha256

        with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
            embedded_path = f"policies/{policy.id}/version-1-policy.pdf"
            assert embedded_path in archive.namelist()


def test_missing_policy_file_on_disk_aborts_the_export(app):
    with app.state.session_factory() as session:
        _define_scope(session, datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))
        policy, version = _make_effective_policy_version(
            session, app.state.settings.data_dir, title="Security Policy", content=b"%PDF-1.4 policy bytes"
        )
        # Simulate the stored file having been lost from disk.
        stored_path = policy_version_path(
            app.state.settings.data_dir, policy.id, version.version_number, version.stored_filename
        )
        os.remove(stored_path)

        with pytest.raises(AuditPackageError):
            generate_audit_package(session, app.state.settings, actor_email="admin@example.com")


def test_manifest_lists_every_embedded_file_with_a_verifiable_hash(app):
    with app.state.session_factory() as session:
        _define_scope(session, datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))
        session.commit()

        package_bytes, manifest_sha256 = generate_audit_package(
            session, app.state.settings, actor_email="admin@example.com"
        )

    with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["disclaimer"]
        assert manifest["generated_by"] == "admin@example.com"
        for entry in manifest["files"]:
            content = archive.read(entry["path"])
            assert hashlib.sha256(content).hexdigest() == entry["sha256"]
        manifest_bytes_without_self = archive.read("manifest.json")
        assert hashlib.sha256(manifest_bytes_without_self).hexdigest() == manifest_sha256


def test_evidence_artifact_missing_from_s3_aborts_the_export(s3_app):
    settings = s3_app.state.settings
    with s3_app.state.session_factory() as session:
        _define_scope(session, datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))
        control = _make_control(session)
        occurrence = record_occurrence_manually(session, control)
        session.commit()
        perform_occurrence(
            session,
            occurrence,
            occurred_at=datetime.datetime(2026, 2, 1),
            performed_by_person_id=None,
            scope_note="done",
            evidence_note="",
        )
        session.commit()

        artifact = EvidenceArtifact(title="Missing evidence")
        session.add(artifact)
        session.flush()
        version = EvidenceArtifactVersion(
            id="ev-missing-1234567890",
            artifact_id=artifact.id,
            version_number=1,
            object_key="does/not/exist.txt",
            original_filename="f.txt",
            media_type="text/plain",
            byte_size=1,
            sha256="a" * 64,
            source_type="manual",
            captured_at=datetime.datetime(2026, 1, 1),
            actor_type="user",
        )
        session.add(version)
        session.commit()
        link_evidence_artifact_version(session, occurrence, version.id)
        session.commit()

        with pytest.raises(AuditPackageError):
            generate_audit_package(session, settings, actor_email="admin@example.com")


def test_evidence_artifact_bytes_embedded_with_matching_hash(s3_app):
    settings = s3_app.state.settings
    with s3_app.state.session_factory() as session:
        _define_scope(session, datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))
        control = _make_control(session)
        occurrence = record_occurrence_manually(session, control)
        session.commit()
        perform_occurrence(
            session,
            occurrence,
            occurred_at=datetime.datetime(2026, 2, 1),
            performed_by_person_id=None,
            scope_note="done",
            evidence_note="",
        )
        session.commit()

        artifact = EvidenceArtifact(title="Screenshot")
        session.add(artifact)
        session.flush()
        content = b"real evidence bytes"
        captured = capture_raw_bytes(content, max_bytes=1024 * 1024)
        version_id = "ev-real-1234567890ab"
        object_key = object_key_for(artifact.id, version_id, "txt")
        client = build_s3_client(settings)
        upload_object(
            client,
            bucket=settings.s3_bucket,
            object_key=object_key,
            tmp_path=captured.tmp_path,
            media_type="text/plain",
        )

        version = EvidenceArtifactVersion(
            id=version_id,
            artifact_id=artifact.id,
            version_number=1,
            object_key=object_key,
            original_filename="f.txt",
            media_type="text/plain",
            byte_size=captured.byte_size,
            sha256=captured.sha256,
            source_type="manual",
            captured_at=datetime.datetime(2026, 1, 1),
            actor_type="user",
        )
        session.add(version)
        session.commit()
        link_evidence_artifact_version(session, occurrence, version.id)
        session.commit()

        package_bytes, _hash = generate_audit_package(session, settings, actor_email="admin@example.com")

    with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
        embedded_path = f"evidence/{artifact.id}/{version_id}-f.txt"
        assert archive.read(embedded_path) == content
