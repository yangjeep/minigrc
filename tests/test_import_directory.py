"""Tests for the watched import directory (Feature 9).

Lifecycle: inbox/ -> processing/ -> archive/completed/ or
archive/rejected/. Claiming is a single atomic os.rename (whichever
process's rename succeeds owns the file — no DB coordination needed for
the file-level claim). Stale processing/ entries (a worker crashed mid-
job) are reconciled back to inbox/ on each pass, making the workflow
recoverable after a restart without a separate heartbeat mechanism.
"""

from __future__ import annotations

import json
import time

from app.import_directory import (
    claim_one_file,
    ensure_directory_layout,
    process_claimed_file,
    reconcile_stale_processing,
    run_directory_once,
)
from app.models import ImportJob, Risk

RISK_CSV = (
    b"title,description,category,likelihood,impact,owner,status\nWatched dir risk,d,security,2,3,me,open\n"
)


def test_ensure_directory_layout_creates_expected_subdirs(tmp_path):
    root = tmp_path / "imports"
    ensure_directory_layout(root)
    assert (root / "inbox").is_dir()
    assert (root / "processing").is_dir()
    assert (root / "archive" / "completed").is_dir()
    assert (root / "archive" / "rejected").is_dir()


def test_claim_one_file_moves_from_inbox_to_processing(tmp_path):
    root = tmp_path / "imports"
    ensure_directory_layout(root)
    (root / "inbox" / "risks.csv").write_bytes(RISK_CSV)

    claimed = claim_one_file(root)
    assert claimed is not None
    assert claimed.parent == root / "processing"
    assert not (root / "inbox" / "risks.csv").exists()


def test_claim_one_file_returns_none_when_inbox_empty(tmp_path):
    root = tmp_path / "imports"
    ensure_directory_layout(root)
    assert claim_one_file(root) is None


def test_second_claim_attempt_on_already_claimed_file_is_safe(tmp_path):
    root = tmp_path / "imports"
    ensure_directory_layout(root)
    (root / "inbox" / "risks.csv").write_bytes(RISK_CSV)

    first = claim_one_file(root)
    assert first is not None
    # Simulate a second worker racing for the same inbox listing: the file
    # is already gone from inbox/, so a second scan finds nothing to claim.
    second = claim_one_file(root)
    assert second is None


def test_process_claimed_file_success_moves_to_archive_completed_with_manifest(app, tmp_path):
    root = tmp_path / "imports"
    ensure_directory_layout(root)
    claimed_path = root / "processing" / "risks.csv"
    claimed_path.write_bytes(RISK_CSV)

    with app.state.session_factory() as session:
        job = process_claimed_file(
            session, claimed_path, importer_name="risk_register_csv", actor="watcher", archive_root=root
        )
        session.commit()
        assert job.status == "completed"

    assert not claimed_path.exists()
    archived = root / "archive" / "completed" / "risks.csv"
    assert archived.exists()
    manifest_path = root / "archive" / "completed" / "risks.csv.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["status"] == "completed"
    assert manifest["import_job_id"] == job.id

    with app.state.session_factory() as session:
        assert session.query(Risk).filter(Risk.title == "Watched dir risk").count() == 1


def test_process_claimed_file_rejection_moves_to_archive_rejected(app, tmp_path):
    root = tmp_path / "imports"
    ensure_directory_layout(root)
    bad_csv = b"title,description,category,likelihood,impact,owner,status\n,d,security,2,3,me,open\n"
    claimed_path = root / "processing" / "bad.csv"
    claimed_path.write_bytes(bad_csv)

    with app.state.session_factory() as session:
        job = process_claimed_file(
            session, claimed_path, importer_name="risk_register_csv", actor="watcher", archive_root=root
        )
        session.commit()
        assert job.status == "rejected"

    assert not claimed_path.exists()
    assert (root / "archive" / "rejected" / "bad.csv").exists()


def test_reconcile_stale_processing_moves_crashed_files_back_to_inbox(tmp_path):
    root = tmp_path / "imports"
    ensure_directory_layout(root)
    stale_file = root / "processing" / "stuck.csv"
    stale_file.write_bytes(RISK_CSV)
    old_time = time.time() - 10_000
    import os

    os.utime(stale_file, (old_time, old_time))

    moved = reconcile_stale_processing(root, stale_seconds=300)
    assert moved == 1
    assert not stale_file.exists()
    assert (root / "inbox" / "stuck.csv").exists()


def test_reconcile_stale_processing_leaves_recent_files_alone(tmp_path):
    root = tmp_path / "imports"
    ensure_directory_layout(root)
    fresh_file = root / "processing" / "fresh.csv"
    fresh_file.write_bytes(RISK_CSV)

    moved = reconcile_stale_processing(root, stale_seconds=300)
    assert moved == 0
    assert fresh_file.exists()


def test_run_directory_once_end_to_end(app, tmp_path):
    root = tmp_path / "imports"
    ensure_directory_layout(root)
    (root / "inbox" / "risks.csv").write_bytes(RISK_CSV)

    processed = run_directory_once(
        app.state.session_factory, root=root, importer_name="risk_register_csv", actor="watcher"
    )
    assert processed is True
    assert (root / "archive" / "completed" / "risks.csv").exists()

    processed_again = run_directory_once(
        app.state.session_factory, root=root, importer_name="risk_register_csv", actor="watcher"
    )
    assert processed_again is False  # nothing left in inbox


def test_duplicate_file_reprocessed_after_restart_is_idempotent(app, tmp_path):
    """A file re-dropped into inbox/ after a restart (same content, same
    checksum) is a safe no-op via the existing ImportJob checksum
    idempotency — it still gets archived as 'completed', just with zero
    new records."""
    root = tmp_path / "imports"
    ensure_directory_layout(root)
    (root / "inbox" / "risks.csv").write_bytes(RISK_CSV)
    run_directory_once(
        app.state.session_factory, root=root, importer_name="risk_register_csv", actor="watcher"
    )

    # Re-drop the same content under a different filename, simulating a
    # re-sync after a restart.
    (root / "inbox" / "risks-resync.csv").write_bytes(RISK_CSV)
    run_directory_once(
        app.state.session_factory, root=root, importer_name="risk_register_csv", actor="watcher"
    )

    with app.state.session_factory() as session:
        assert session.query(Risk).filter(Risk.title == "Watched dir risk").count() == 1
        jobs = session.query(ImportJob).filter_by(importer_name="risk_register_csv").all()
        assert len(jobs) == 2
        assert jobs[1].records_skipped == 1
