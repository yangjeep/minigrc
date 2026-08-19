"""Integration tests for app/evidence_repository.py (issue #32), backed
by moto's in-process, real S3-API-compatible mock — satisfies the
issue's "exercise a selected S3-compatible test backend" requirement
without a live Docker/MinIO service container (see design doc §7).
"""

from __future__ import annotations

import os

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import select

from app.events import DomainEvent
from app.evidence_repository import capture_evidence_version, rebuild_evidence_artifact_version_projection
from app.models import EvidenceArtifact, EvidenceArtifactVersion
from app.object_storage import ObjectStorageError, capture_raw_bytes, download_object

BUCKET = "minigrc-evidence-test"


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def _make_artifact(session, title="Test Artifact"):
    artifact = EvidenceArtifact(title=title)
    session.add(artifact)
    session.flush()
    return artifact


def test_capture_creates_first_version(app, s3):
    with app.state.session_factory() as session:
        artifact = _make_artifact(session)
        captured = capture_raw_bytes(b"evidence content", max_bytes=1024 * 1024)
        version, created = capture_evidence_version(
            session,
            artifact,
            client=s3,
            bucket=BUCKET,
            tmp_path=captured.tmp_path,
            sha256=captured.sha256,
            byte_size=captured.byte_size,
            media_type="text/plain",
            original_filename="evidence.txt",
            extension="txt",
            actor_id="user-1",
        )
        session.commit()

        assert created is True
        assert version.version_number == 1
        assert version.sha256 == captured.sha256
        assert version.artifact_id == artifact.id
        assert not os.path.exists(captured.tmp_path)  # cleaned up after upload


def test_capture_uploads_bytes_that_round_trip_identically(app, s3):
    with app.state.session_factory() as session:
        artifact = _make_artifact(session)
        content = b"the exact bytes that must round-trip"
        captured = capture_raw_bytes(content, max_bytes=1024 * 1024)
        version, _created = capture_evidence_version(
            session,
            artifact,
            client=s3,
            bucket=BUCKET,
            tmp_path=captured.tmp_path,
            sha256=captured.sha256,
            byte_size=captured.byte_size,
            media_type="text/plain",
            original_filename="evidence.txt",
            extension="txt",
            actor_id="user-1",
        )
        session.commit()

        downloaded = download_object(s3, bucket=BUCKET, object_key=version.object_key)
        assert downloaded == content


def test_duplicate_unchanged_capture_is_idempotent(app, s3):
    """Same bytes captured twice for the same artifact: no new version,
    no new event, and the second (redundant) local temp file is cleaned
    up without ever being uploaded."""
    with app.state.session_factory() as session:
        artifact = _make_artifact(session)
        content = b"unchanged evidence"

        captured1 = capture_raw_bytes(content, max_bytes=1024 * 1024)
        version1, created1 = capture_evidence_version(
            session,
            artifact,
            client=s3,
            bucket=BUCKET,
            tmp_path=captured1.tmp_path,
            sha256=captured1.sha256,
            byte_size=captured1.byte_size,
            media_type="text/plain",
            original_filename="evidence.txt",
            extension="txt",
            actor_id="user-1",
        )
        session.commit()
        session.refresh(artifact)

        captured2 = capture_raw_bytes(content, max_bytes=1024 * 1024)
        version2, created2 = capture_evidence_version(
            session,
            artifact,
            client=s3,
            bucket=BUCKET,
            tmp_path=captured2.tmp_path,
            sha256=captured2.sha256,
            byte_size=captured2.byte_size,
            media_type="text/plain",
            original_filename="evidence.txt",
            extension="txt",
            actor_id="user-1",
        )

        assert created1 is True
        assert created2 is False
        assert version2.id == version1.id
        assert not os.path.exists(captured2.tmp_path)

        versions = session.scalars(
            select(EvidenceArtifactVersion).where(EvidenceArtifactVersion.artifact_id == artifact.id)
        ).all()
        assert len(versions) == 1

        events = session.scalars(
            select(DomainEvent).where(
                DomainEvent.aggregate_type == "evidence_artifact_version",
                DomainEvent.aggregate_id == version1.id,
            )
        ).all()
        assert len(events) == 1


def test_changed_bytes_create_a_new_immutable_version(app, s3):
    with app.state.session_factory() as session:
        artifact = _make_artifact(session)

        captured1 = capture_raw_bytes(b"version one content", max_bytes=1024 * 1024)
        version1, _ = capture_evidence_version(
            session,
            artifact,
            client=s3,
            bucket=BUCKET,
            tmp_path=captured1.tmp_path,
            sha256=captured1.sha256,
            byte_size=captured1.byte_size,
            media_type="text/plain",
            original_filename="v1.txt",
            extension="txt",
            actor_id="user-1",
        )
        session.commit()
        session.refresh(artifact)
        original_sha256, original_object_key = version1.sha256, version1.object_key

        captured2 = capture_raw_bytes(b"version two, different content", max_bytes=1024 * 1024)
        version2, created2 = capture_evidence_version(
            session,
            artifact,
            client=s3,
            bucket=BUCKET,
            tmp_path=captured2.tmp_path,
            sha256=captured2.sha256,
            byte_size=captured2.byte_size,
            media_type="text/plain",
            original_filename="v2.txt",
            extension="txt",
            actor_id="user-1",
        )
        session.commit()

        assert created2 is True
        assert version2.id != version1.id
        assert version2.version_number == 2

        # First version's bytes/hash are unchanged by the second capture.
        session.refresh(version1)
        assert version1.sha256 == original_sha256
        assert version1.object_key == original_object_key
        assert download_object(s3, bucket=BUCKET, object_key=version1.object_key) == b"version one content"


def test_object_store_failure_leaves_no_db_row_or_event(app, s3):
    """Upload to a bucket that doesn't exist — moto raises the same
    class of error a real misconfigured/unreachable S3 target would."""
    with app.state.session_factory() as session:
        artifact = _make_artifact(session)
        captured = capture_raw_bytes(b"content", max_bytes=1024 * 1024)

        with pytest.raises(ObjectStorageError):
            capture_evidence_version(
                session,
                artifact,
                client=s3,
                bucket="this-bucket-does-not-exist",
                tmp_path=captured.tmp_path,
                sha256=captured.sha256,
                byte_size=captured.byte_size,
                media_type="text/plain",
                original_filename="evidence.txt",
                extension="txt",
                actor_id="user-1",
            )

        assert not os.path.exists(captured.tmp_path)  # cleaned up even on failure
        versions = session.scalars(select(EvidenceArtifactVersion)).all()
        assert versions == []
        events = session.scalars(
            select(DomainEvent).where(DomainEvent.aggregate_type == "evidence_artifact_version")
        ).all()
        assert events == []


def _capture_with_broken_projector(session, artifact, s3, monkeypatch):
    import app.evidence_repository as evidence_repository_module

    def _broken_projector(session, event):
        raise RuntimeError("simulated projector failure")

    monkeypatch.setitem(
        evidence_repository_module.EVIDENCE_ARTIFACT_VERSION_PROJECTORS,
        "EvidenceArtifactVersionCaptured",
        _broken_projector,
    )
    captured = capture_raw_bytes(b"content that uploads fine", max_bytes=1024 * 1024)
    with pytest.raises(RuntimeError, match="simulated projector failure"):
        capture_evidence_version(
            session,
            artifact,
            client=s3,
            bucket=BUCKET,
            tmp_path=captured.tmp_path,
            sha256=captured.sha256,
            byte_size=captured.byte_size,
            media_type="text/plain",
            original_filename="evidence.txt",
            extension="txt",
            actor_id="user-1",
        )


def test_db_failure_after_successful_upload_cleans_up_the_orphan_object(app, s3, monkeypatch):
    """The reverse ordering failure (design doc §4): the S3 upload
    succeeds, but the subsequent event append/projection fails — the
    just-uploaded object must not be left referencing a DB row/event
    that was never created. This is capture_evidence_version's own
    explicit `delete_object` cleanup (see its docstring), independent
    of the SQL transaction entirely — unaffected by the deferred
    pysqlite/SAVEPOINT bug below.
    """
    with app.state.session_factory() as session:
        artifact = _make_artifact(session)
        session.commit()
        _capture_with_broken_projector(session, artifact, s3, monkeypatch)

    objects = s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])
    assert objects == []


@pytest.mark.xfail(
    reason="Known, deferred pysqlite/SAVEPOINT rollback bug (issue #65) — see "
    "tests/test_sqlite_savepoint_rollback.py's module docstring. The event's "
    "own DomainEvent row (appended via a SAVEPOINT that already released "
    "before the projector failure) does not actually get undone by "
    "session.rollback() in this specific sequence today.",
    strict=True,
)
def test_db_row_does_not_survive_rollback_after_projector_failure(app, s3, monkeypatch):
    """append_and_project's event insert happens in its own SAVEPOINT,
    independent of the projector that runs after it — so the DomainEvent
    row is still flushed/pending in this session immediately after the
    exception, exactly like any other domain function in this codebase
    (make_version_effective, perform_occurrence, ...): a full rollback
    on any exception is the caller's responsibility
    (app/deps.py::get_db does this for every real request), not
    something capture_evidence_version does itself. This test's
    explicit session.rollback() reproduces that real request-lifecycle
    guarantee — which is exactly what the deferred bug breaks."""
    with app.state.session_factory() as session:
        artifact = _make_artifact(session)
        session.commit()
        _capture_with_broken_projector(session, artifact, s3, monkeypatch)
        session.rollback()

        versions = session.scalars(select(EvidenceArtifactVersion)).all()
        assert versions == []
        events = session.scalars(
            select(DomainEvent).where(DomainEvent.aggregate_type == "evidence_artifact_version")
        ).all()
        assert events == []


def test_rebuild_evidence_artifact_version_projection_reproduces_state(app, s3):
    with app.state.session_factory() as session:
        artifact = _make_artifact(session)

        captured1 = capture_raw_bytes(b"first version", max_bytes=1024 * 1024)
        version1, _ = capture_evidence_version(
            session,
            artifact,
            client=s3,
            bucket=BUCKET,
            tmp_path=captured1.tmp_path,
            sha256=captured1.sha256,
            byte_size=captured1.byte_size,
            media_type="text/plain",
            original_filename="v1.txt",
            extension="txt",
            actor_id="user-1",
        )
        session.commit()
        session.refresh(artifact)

        captured2 = capture_raw_bytes(b"second version", max_bytes=1024 * 1024)
        version2, _ = capture_evidence_version(
            session,
            artifact,
            client=s3,
            bucket=BUCKET,
            tmp_path=captured2.tmp_path,
            sha256=captured2.sha256,
            byte_size=captured2.byte_size,
            media_type="text/plain",
            original_filename="v2.txt",
            extension="txt",
            actor_id="user-1",
        )
        session.commit()
        version_ids = [version1.id, version2.id]
        # Refresh from the DB before snapshotting "before" — see #31's
        # identical lesson (tz-aware in-memory vs. tz-naive round-tripped
        # values would otherwise be compared and spuriously differ).
        before = {
            v.id: (v.sha256, v.version_number, v.object_key, v.captured_at, v.actor_id)
            for v in (session.get(EvidenceArtifactVersion, vid) for vid in version_ids)
        }

        rebuild_evidence_artifact_version_projection(session)
        session.commit()

        # The rebuild deletes-and-reinserts rows (unlike #31's PolicyVersion,
        # which only UPDATEs existing rows) — session.refresh() on the old
        # Python object would fail with "not persistent"; fetch fresh
        # objects by id instead.
        after = {
            vid: (v.sha256, v.version_number, v.object_key, v.captured_at, v.actor_id)
            for vid in version_ids
            for v in [session.get(EvidenceArtifactVersion, vid)]
        }
        assert before == after


def test_rebuild_does_not_touch_uninvolved_artifacts(app, s3):
    """A sanity guard: rebuilding wipes and replays ALL
    evidence_artifact_version events globally, not scoped to one
    artifact — confirm a second, untouched artifact's version survives
    identically too."""
    with app.state.session_factory() as session:
        artifact_a = _make_artifact(session, title="Artifact A")
        artifact_b = _make_artifact(session, title="Artifact B")

        captured_a = capture_raw_bytes(b"artifact a content", max_bytes=1024 * 1024)
        version_a, _ = capture_evidence_version(
            session,
            artifact_a,
            client=s3,
            bucket=BUCKET,
            tmp_path=captured_a.tmp_path,
            sha256=captured_a.sha256,
            byte_size=captured_a.byte_size,
            media_type="text/plain",
            original_filename="a.txt",
            extension="txt",
            actor_id="user-1",
        )
        captured_b = capture_raw_bytes(b"artifact b content", max_bytes=1024 * 1024)
        version_b, _ = capture_evidence_version(
            session,
            artifact_b,
            client=s3,
            bucket=BUCKET,
            tmp_path=captured_b.tmp_path,
            sha256=captured_b.sha256,
            byte_size=captured_b.byte_size,
            media_type="text/plain",
            original_filename="b.txt",
            extension="txt",
            actor_id="user-1",
        )
        session.commit()

        rebuild_evidence_artifact_version_projection(session)
        session.commit()

        rebuilt_a = session.get(EvidenceArtifactVersion, version_a.id)
        rebuilt_b = session.get(EvidenceArtifactVersion, version_b.id)
        assert rebuilt_a.sha256 == captured_a.sha256
        assert rebuilt_b.sha256 == captured_b.sha256
