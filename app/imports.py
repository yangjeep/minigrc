"""Native import subsystem: a shared, checksum-idempotent, all-or-nothing
importer registry used by web uploads, the CLI, and (Feature 9) the
watched import directory.

Threat model (see the architecture checkpoint on umbrella issue #5):
imported file content is always untrusted. Every importer validates every
row before writing anything (existing pattern from
app/csv_import.py::import_requirements_csv, reused here rather than
reimplemented); free-text CSV values are neutralized against formula
injection (CSV/Excel/Sheets "=cmd|...", per OWASP CSV injection guidance)
before they're stored, since this app's own register grids could later
export/display them in a spreadsheet-adjacent context; reads are bounded
via app/uploads.py's existing chunked-read cap; a file's SHA-256 makes
re-submitting the exact same content a safe no-op rather than a duplicate
import.
"""

from __future__ import annotations

import base64
import csv
import dataclasses
import datetime
import hashlib
import io
import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.csv_import import import_requirements_csv
from app.jobs import claim_specific_job, enqueue_job, register_handler, run_job
from app.models import RISK_STATUSES, Framework, ImportJob, Risk
from app.uploads import UploadTooLargeError

_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@")


def neutralize_csv_formula(value: str) -> str:
    """Prefix a leading formula-trigger character with a single quote.

    Excel/Sheets/LibreOffice treat a cell starting with =, +, -, or @ as a
    formula; a crafted cell like `=cmd|'/c calc'!A1` can execute when a
    human later opens an exported CSV in a spreadsheet application. The
    leading quote forces the cell to render as literal text instead.
    """
    if value and value[0] in _FORMULA_TRIGGER_CHARS:
        return "'" + value
    return value


def compute_checksum(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


@dataclasses.dataclass(frozen=True)
class ImportResult:
    discovered: int
    created: int
    updated: int = 0
    skipped: int = 0


ImporterFn = Callable[[Session, bytes, dict[str, Any]], ImportResult]

_IMPORTERS: dict[str, tuple[str, ImporterFn]] = {}  # name -> (entity_type, fn)


def register_importer(name: str, *, entity_type: str, fn: ImporterFn) -> None:
    _IMPORTERS[name] = (entity_type, fn)


def _decode_csv_text(raw_bytes: bytes) -> str:
    return raw_bytes.decode("utf-8-sig")


def _import_framework_requirements(
    session: Session, raw_bytes: bytes, target: dict[str, Any]
) -> ImportResult:
    framework = session.get(Framework, target.get("framework_id"))
    if framework is None:
        raise ValueError(f"framework {target.get('framework_id')} not found")
    created, errors = import_requirements_csv(session, framework, raw_bytes)
    if errors:
        raise ValueError("; ".join(errors[:10]))
    return ImportResult(discovered=created, created=created)


register_importer(
    "framework_requirements_csv", entity_type="framework_requirement", fn=_import_framework_requirements
)


def _import_risk_register(session: Session, raw_bytes: bytes, target: dict[str, Any]) -> ImportResult:
    text = _decode_csv_text(raw_bytes)
    reader = csv.DictReader(io.StringIO(text))
    required = {"title"}
    fieldnames = set(reader.fieldnames or [])
    missing = required - fieldnames
    if missing:
        raise ValueError(f"Missing required column(s): {', '.join(sorted(missing))}")

    parsed: list[dict[str, Any]] = []
    errors: list[str] = []
    for line_number, row in enumerate(reader, start=2):
        title = (row.get("title") or "").strip()
        if not title:
            errors.append(f"Row {line_number}: title is required")
            continue
        status = (row.get("status") or "open").strip()
        if status not in RISK_STATUSES:
            errors.append(f"Row {line_number}: status must be one of {', '.join(RISK_STATUSES)}")
            continue
        try:
            likelihood = int((row.get("likelihood") or "1").strip())
            impact = int((row.get("impact") or "1").strip())
        except ValueError:
            errors.append(f"Row {line_number}: likelihood/impact must be integers")
            continue
        if not (1 <= likelihood <= 5) or not (1 <= impact <= 5):
            errors.append(f"Row {line_number}: likelihood/impact must be between 1 and 5")
            continue

        parsed.append(
            {
                "title": neutralize_csv_formula(title),
                "description": neutralize_csv_formula((row.get("description") or "").strip()),
                "category": neutralize_csv_formula((row.get("category") or "general").strip()),
                "likelihood": likelihood,
                "impact": impact,
                "owner": neutralize_csv_formula((row.get("owner") or "").strip()),
                "status": status,
                "treatment_plan": neutralize_csv_formula((row.get("treatment_plan") or "").strip()),
            }
        )

    if errors:
        raise ValueError("; ".join(errors[:10]))
    if not parsed:
        raise ValueError("CSV file contained no data rows.")

    for fields in parsed:
        session.add(Risk(**fields))
    return ImportResult(discovered=len(parsed), created=len(parsed))


register_importer("risk_register_csv", entity_type="risk", fn=_import_risk_register)


def run_import(
    session: Session,
    *,
    importer_name: str,
    raw_bytes: bytes,
    filename: str,
    target: dict[str, Any],
    actor: str,
    source: str,
    max_bytes: int = 25 * 1024 * 1024,
) -> ImportJob:
    now = datetime.datetime.now(datetime.UTC)
    job = ImportJob(
        source=source,
        importer_name=importer_name,
        original_filename=filename,
        file_size=len(raw_bytes),
        status="validating",
        target_json=json.dumps(target),
        started_at=now,
        created_by=actor,
    )
    session.add(job)
    session.flush()

    def _reject(message: str) -> ImportJob:
        job.status = "rejected"
        job.validation_errors_json = json.dumps([message])
        job.completed_at = datetime.datetime.now(datetime.UTC)
        record_audit_event(
            session,
            entity_type="import_job",
            entity_id=job.id,
            action="reject",
            detail=f"Import '{filename}' rejected: {message}",
            actor=actor,
        )
        return job

    if len(raw_bytes) > max_bytes:
        return _reject(f"File exceeds the maximum size of {max_bytes // (1024 * 1024)} MB.")

    checksum = compute_checksum(raw_bytes)
    job.checksum_sha256 = checksum

    duplicate = session.scalar(
        select(ImportJob).where(
            ImportJob.checksum_sha256 == checksum,
            ImportJob.importer_name == importer_name,
            ImportJob.target_json == json.dumps(target),
            ImportJob.status == "completed",
            ImportJob.id != job.id,
        )
    )
    if duplicate is not None:
        job.status = "completed"
        job.records_skipped = 1
        job.validation_errors_json = json.dumps([f"Duplicate of already-completed import job {duplicate.id}"])
        job.completed_at = datetime.datetime.now(datetime.UTC)
        record_audit_event(
            session,
            entity_type="import_job",
            entity_id=job.id,
            action="skip_duplicate",
            detail=f"Import '{filename}' skipped — identical to job {duplicate.id}",
            actor=actor,
        )
        return job

    entry = _IMPORTERS.get(importer_name)
    if entry is None:
        return _reject(f"Unknown importer '{importer_name}'")
    entity_type, fn = entry
    job.entity_type = entity_type

    try:
        text_check = raw_bytes.decode("utf-8-sig")  # cheap encoding gate before importer-specific parsing
        del text_check
    except UnicodeDecodeError:
        return _reject("File is not valid UTF-8 text.")

    job.status = "importing"
    try:
        result = fn(session, raw_bytes, target)
        session.flush()
    except Exception as exc:  # noqa: BLE001 - any importer failure means nothing was written
        session.rollback()
        # job row itself was rolled back too — re-add it in rejected state
        job = ImportJob(
            source=source,
            importer_name=importer_name,
            original_filename=filename,
            file_size=len(raw_bytes),
            checksum_sha256=checksum,
            status="rejected",
            target_json=json.dumps(target),
            validation_errors_json=json.dumps([str(exc)[:2000]]),
            started_at=now,
            completed_at=datetime.datetime.now(datetime.UTC),
            created_by=actor,
        )
        session.add(job)
        session.flush()
        record_audit_event(
            session,
            entity_type="import_job",
            entity_id=job.id,
            action="reject",
            detail=f"Import '{filename}' rejected: {exc}",
            actor=actor,
        )
        return job

    job.status = "completed"
    job.records_discovered = result.discovered
    job.records_created = result.created
    job.records_updated = result.updated
    job.records_skipped = result.skipped
    job.completed_at = datetime.datetime.now(datetime.UTC)
    record_audit_event(
        session,
        entity_type="import_job",
        entity_id=job.id,
        action="complete",
        detail=f"Import '{filename}' via {importer_name}: created {result.created}",
        actor=actor,
    )
    return job


def _run_import_job_handler(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    raw_bytes = base64.b64decode(payload["raw_bytes_b64"])
    job = run_import(
        session,
        importer_name=payload["importer_name"],
        raw_bytes=raw_bytes,
        filename=payload["filename"],
        target=payload["target"],
        actor=payload["actor"],
        source=payload["source"],
    )
    return {"import_job_id": job.id, "status": job.status}


register_handler("run_import", _run_import_job_handler)


def enqueue_and_run_import(
    session: Session,
    *,
    importer_name: str,
    raw_bytes: bytes,
    filename: str,
    target: dict[str, Any],
    actor: str,
    source: str,
) -> ImportJob:
    """Run an import through the Feature 7 job system (same synchronous-
    inline pattern as the Feature 6 connection test): enqueue a job, claim
    exactly that job, run it, then look up the resulting ImportJob row.
    """
    job = enqueue_job(
        session,
        job_type="run_import",
        payload={
            "importer_name": importer_name,
            "raw_bytes_b64": base64.b64encode(raw_bytes).decode("ascii"),
            "filename": filename,
            "target": target,
            "actor": actor,
            "source": source,
        },
        actor=actor,
    )
    session.flush()
    claimed = claim_specific_job(session, job.id, worker_id="inline")
    if claimed is not None:
        run_job(session, claimed, actor=actor)
        session.flush()
        job = claimed

    if job.status == "succeeded" and job.result_json:
        result = json.loads(job.result_json)
        import_job = session.get(ImportJob, result["import_job_id"])
        if import_job is not None:
            return import_job

    # Job-infrastructure failure (not a normal import rejection, which
    # always returns an ImportJob above) — surface a minimal ImportJob-
    # shaped record so callers have one consistent return type.
    return ImportJob(
        id="",
        source=source,
        importer_name=importer_name,
        original_filename=filename,
        status="rejected",
        validation_errors_json=json.dumps([job.error_message or "Import job failed to run."]),
        created_by=actor,
    )


__all__ = [
    "ImportResult",
    "UploadTooLargeError",
    "compute_checksum",
    "enqueue_and_run_import",
    "neutralize_csv_formula",
    "register_importer",
    "run_import",
]
