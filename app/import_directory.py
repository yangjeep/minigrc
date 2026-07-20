"""Watched import directory (Feature 9): inbox/ -> processing/ ->
archive/completed/ or archive/rejected/.

Claiming a file is a single `os.rename` — atomic on the same filesystem,
so whichever process's rename call succeeds owns the file; a second racing
attempt on an already-moved file simply finds nothing left to claim. This
gives safe multi-worker semantics for the file-level claim without any
database coordination. Duplicate-across-restarts protection is inherited
for free from Feature 8's checksum-based ImportJob idempotency — re-
dropping the same file content after a crash/restart is a safe no-op, not
a duplicate import.

A worker that crashes after claiming (moving inbox -> processing) but
before finishing leaves a stale file in processing/; `reconcile_stale_processing`
moves anything older than a threshold back to inbox/ so the next pass
picks it up — this is the crash-recovery mechanism, not a heartbeat.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from app.audit import record_audit_event
from app.imports import run_import

DEFAULT_STALE_SECONDS = 300


def ensure_directory_layout(root: Path) -> None:
    for sub in ("inbox", "processing", "archive/completed", "archive/rejected"):
        (root / sub).mkdir(parents=True, exist_ok=True)


def _is_file_stable(path: Path, *, wait_seconds: float = 0.2) -> bool:
    """A file mid-write (e.g. still being synced in) has a changing size —
    check twice a short interval apart before claiming it."""
    try:
        size_before = path.stat().st_size
    except FileNotFoundError:
        return False
    time.sleep(wait_seconds)
    try:
        size_after = path.stat().st_size
    except FileNotFoundError:
        return False
    return size_before == size_after


def claim_one_file(root: Path) -> Path | None:
    ensure_directory_layout(root)
    inbox = root / "inbox"
    processing = root / "processing"
    candidates = sorted(p for p in inbox.iterdir() if p.is_file())
    for candidate in candidates:
        if not _is_file_stable(candidate):
            continue
        target = processing / candidate.name
        try:
            os.rename(candidate, target)
        except FileNotFoundError:
            continue  # another process claimed it between our listing and rename
        return target
    return None


def reconcile_stale_processing(root: Path, *, stale_seconds: int = DEFAULT_STALE_SECONDS) -> int:
    ensure_directory_layout(root)
    processing = root / "processing"
    inbox = root / "inbox"
    now = time.time()
    moved = 0
    for path in processing.iterdir():
        if not path.is_file():
            continue
        if now - path.stat().st_mtime > stale_seconds:
            os.rename(path, inbox / path.name)
            moved += 1
    return moved


def process_claimed_file(session, path: Path, *, importer_name: str, actor: str, archive_root: Path):
    ensure_directory_layout(archive_root)
    raw_bytes = path.read_bytes()
    job = run_import(
        session,
        importer_name=importer_name,
        raw_bytes=raw_bytes,
        filename=path.name,
        target={},
        actor=actor,
        source="watched_directory",
    )
    session.flush()

    destination_dir = archive_root / "archive" / ("completed" if job.status == "completed" else "rejected")
    destination = destination_dir / path.name
    os.rename(path, destination)
    manifest = {
        "import_job_id": job.id,
        "status": job.status,
        "records_created": job.records_created,
        "records_skipped": job.records_skipped,
        "checksum_sha256": job.checksum_sha256,
        "original_filename": path.name,
    }
    (destination_dir / f"{path.name}.manifest.json").write_text(json.dumps(manifest, indent=2))

    record_audit_event(
        session,
        entity_type="import_job",
        entity_id=job.id,
        action="watched_directory_archive",
        detail=f"Archived '{path.name}' to {destination_dir.name}/ ({job.status})",
        actor=actor,
    )
    return job


def run_directory_once(session_factory, *, root: Path, importer_name: str, actor: str) -> bool:
    """One reconcile+claim+process pass. Returns True if a file was
    processed, False if there was nothing to do — the caller (worker loop
    or CLI) decides whether/how long to wait before the next pass."""
    reconcile_stale_processing(root)
    claimed = claim_one_file(root)
    if claimed is None:
        return False
    with session_factory() as session:
        process_claimed_file(session, claimed, importer_name=importer_name, actor=actor, archive_root=root)
        session.commit()
    return True
