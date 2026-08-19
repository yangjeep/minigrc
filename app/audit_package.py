"""Defensible SOC 2 audit/PBC package export (issue #34).

A plain, compute-and-return module — matching the shape of
app/readiness.py/app/vendor_flags.py, nothing persisted server-side (see
docs/superpowers/specs/2026-08-19-issue34-audit-pbc-package-design.md §2.2
for why). Exports against the current ComplianceScope's declared audit
period (#30) — org-wide singulars (frameworks/controls/policies) are
included in full; occurrences/tests/findings are filtered to what's
materially relevant to that period (see design doc §2.3 for the exact
rules). Reuses the exact retrieval mechanisms
app/routers/evidence_artifacts.py and app/routers/policies.py already use
for single-file downloads — this module only adds the zip/manifest
assembly and the "fail the whole export, not partially" discipline.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import os
import zipfile

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.compliance_scope import get_compliance_scope
from app.config import Settings
from app.models import (
    ComplianceScope,
    ControlOccurrence,
    ControlOccurrenceEvidence,
    ControlOccurrenceEvidenceArtifact,
    ControlRequirementMapping,
    ControlTest,
    ControlTestEvidenceArtifact,
    ControlTestException,
    EvidenceArtifactVersion,
    EvidenceSnapshot,
    Finding,
    FindingRemediationUpdate,
    FindingRetest,
    Framework,
    InternalControl,
    Policy,
    PolicyControlMapping,
    PolicyVersion,
)
from app.object_storage import (
    ObjectStorageError,
    ObjectStorageNotConfiguredError,
    build_s3_client,
    download_object,
)
from app.storage import policy_version_path

DISCLAIMER = (
    "This package is a factual export of miniGRC's authoritative compliance records. "
    "It is not an auditor's opinion, attestation, or certification of compliance."
)


class AuditPackageError(RuntimeError):
    """A safe-to-display reason package generation failed (e.g. no
    compliance scope defined, or a missing/corrupt evidence or policy
    file) — never includes credential material."""


def _iso(value: datetime.date | datetime.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _occurrence_in_period(occurrence: ControlOccurrence, start: datetime.date, end: datetime.date) -> bool:
    if occurrence.due_at is not None and start <= occurrence.due_at.date() <= end:
        return True
    return occurrence.performed_at is not None and start <= occurrence.performed_at.date() <= end


def _test_in_period(test: ControlTest, start: datetime.date, end: datetime.date) -> bool:
    return start <= test.performed_at.date() <= end


def _policy_versions_for_period(
    session: Session, policy: Policy, start: datetime.date, end: datetime.date
) -> list[PolicyVersion]:
    return [
        version
        for version in policy.versions
        if version.effective_at is not None
        and version.effective_at.date() <= end
        and (version.superseded_at is None or version.superseded_at.date() >= start)
    ]


def _finding_in_period(
    finding: Finding, start: datetime.date, end: datetime.date, in_period_test_ids: set
) -> bool:
    if finding.source_test_id in in_period_test_ids:
        return True
    if start <= finding.created_at.date() <= end:
        return True
    return finding.status != "closed"


def _serialize_scope(scope: ComplianceScope) -> dict:
    return {
        "service_description": scope.service_description,
        "trust_service_categories": scope.trust_service_categories.split(","),
        "audit_period_starts_on": _iso(scope.audit_period_starts_on),
        "audit_period_ends_on": _iso(scope.audit_period_ends_on),
        "data_categories": scope.data_categories,
        "locations": scope.locations,
        "exclusions": scope.exclusions,
        "exclusions_rationale": scope.exclusions_rationale,
        "revision": scope.revision,
    }


def _serialize_frameworks(session: Session) -> list[dict]:
    frameworks = session.scalars(select(Framework).where(Framework.is_active.is_(True))).all()
    return [
        {
            "id": framework.id,
            "name": framework.name,
            "version": framework.version,
            "is_primary": framework.is_primary,
            "catalog_key": framework.catalog_key,
            "requirements": [
                {"id": r.id, "reference_code": r.reference_code, "title": r.title, "summary": r.summary}
                for r in framework.requirements
            ],
        }
        for framework in frameworks
    ]


def _serialize_controls(session: Session) -> list[dict]:
    controls = session.scalars(select(InternalControl)).all()
    mappings = session.scalars(select(ControlRequirementMapping)).all()
    requirement_ids_by_control: dict[str, list[str]] = {}
    for mapping in mappings:
        requirement_ids_by_control.setdefault(mapping.control_id, []).append(mapping.requirement_id)
    return [
        {
            "id": control.id,
            "name": control.name,
            "description": control.description,
            "owner": control.owner,
            "owner_person_id": control.owner_person_id,
            "status": control.status,
            "cadence_type": control.cadence_type,
            "cadence_interval_months": control.cadence_interval_months,
            "mapped_requirement_ids": requirement_ids_by_control.get(control.id, []),
        }
        for control in controls
    ]


def _serialize_occurrences(occurrences: list[ControlOccurrence], session: Session) -> list[dict]:
    result = []
    for occurrence in occurrences:
        snapshot_links = session.scalars(
            select(ControlOccurrenceEvidence).where(ControlOccurrenceEvidence.occurrence_id == occurrence.id)
        ).all()
        artifact_links = session.scalars(
            select(ControlOccurrenceEvidenceArtifact).where(
                ControlOccurrenceEvidenceArtifact.occurrence_id == occurrence.id
            )
        ).all()
        result.append(
            {
                "id": occurrence.id,
                "control_id": occurrence.control_id,
                "control_period_id": occurrence.control_period_id,
                "origin": occurrence.origin,
                "due_at": _iso(occurrence.due_at),
                "performed_at": _iso(occurrence.performed_at),
                "performed_by_person_id": occurrence.performed_by_person_id,
                "scope_note": occurrence.scope_note,
                "evidence_note": occurrence.evidence_note,
                "linked_evidence_snapshot_ids": [link.evidence_snapshot_id for link in snapshot_links],
                "linked_evidence_artifact_version_ids": [
                    link.evidence_artifact_version_id for link in artifact_links
                ],
            }
        )
    return result


def _serialize_tests(tests: list[ControlTest], session: Session) -> list[dict]:
    result = []
    for test in tests:
        exceptions = session.scalars(
            select(ControlTestException).where(ControlTestException.test_id == test.id)
        ).all()
        evidence_links = session.scalars(
            select(ControlTestEvidenceArtifact).where(ControlTestEvidenceArtifact.test_id == test.id)
        ).all()
        result.append(
            {
                "id": test.id,
                "control_id": test.control_id,
                "control_period_id": test.control_period_id,
                "sample_id": test.sample_id,
                "procedure_description": test.procedure_description,
                "tester_person_id": test.tester_person_id,
                "performed_at": _iso(test.performed_at),
                "result": test.result,
                "notes": test.notes,
                "retest_of_test_id": test.retest_of_test_id,
                "exceptions": [
                    {"sample_item_id": e.sample_item_id, "description": e.description} for e in exceptions
                ],
                "linked_evidence_artifact_version_ids": [
                    e.evidence_artifact_version_id for e in evidence_links
                ],
            }
        )
    return result


def _serialize_findings(findings: list[Finding], session: Session) -> list[dict]:
    result = []
    for finding in findings:
        updates = session.scalars(
            select(FindingRemediationUpdate)
            .where(FindingRemediationUpdate.finding_id == finding.id)
            .order_by(FindingRemediationUpdate.created_at)
        ).all()
        retests = session.scalars(select(FindingRetest).where(FindingRetest.finding_id == finding.id)).all()
        result.append(
            {
                "id": finding.id,
                "title": finding.title,
                "description": finding.description,
                "severity": finding.severity,
                "status": finding.status,
                "control_id": finding.control_id,
                "source_test_id": finding.source_test_id,
                "owner_person_id": finding.owner_person_id,
                "due_date": _iso(finding.due_date),
                "closure_decision": finding.closure_decision,
                "closure_note": finding.closure_note,
                "created_at": _iso(finding.created_at),
                "remediation_updates": [
                    {"note": u.note, "actor": u.actor, "created_at": _iso(u.created_at)} for u in updates
                ],
                "retest_test_ids": [r.test_id for r in retests],
            }
        )
    return result


def generate_audit_package(session: Session, settings: Settings, *, actor_email: str) -> tuple[bytes, str]:
    """Returns (zip_bytes, manifest_sha256). Raises AuditPackageError and
    writes nothing if any required artifact cannot be retrieved/verified,
    or if no compliance scope has been defined yet."""
    scope = get_compliance_scope(session)
    if scope is None:
        raise AuditPackageError("No compliance scope has been defined yet — nothing to export.")
    if scope.audit_period_starts_on is None or scope.audit_period_ends_on is None:
        raise AuditPackageError("The compliance scope has no audit period set — nothing to export.")
    start, end = scope.audit_period_starts_on, scope.audit_period_ends_on

    all_occurrences = session.scalars(select(ControlOccurrence)).all()
    occurrences = [o for o in all_occurrences if _occurrence_in_period(o, start, end)]

    all_tests = session.scalars(select(ControlTest)).all()
    tests = [t for t in all_tests if _test_in_period(t, start, end)]
    in_period_test_ids = {t.id for t in tests}

    all_findings = session.scalars(select(Finding)).all()
    findings = [f for f in all_findings if _finding_in_period(f, start, end, in_period_test_ids)]

    buffer = io.BytesIO()
    manifest_entries: list[dict] = []

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:

        def write_json(path: str, payload) -> None:
            data = json.dumps(payload, indent=2, default=str).encode("utf-8")
            archive.writestr(path, data)
            manifest_entries.append({"path": path, "sha256": _sha256(data), "byte_size": len(data)})

        write_json("scope.json", _serialize_scope(scope))
        write_json("frameworks.json", _serialize_frameworks(session))
        write_json("controls.json", _serialize_controls(session))
        write_json("occurrences.json", _serialize_occurrences(occurrences, session))
        write_json("control_tests.json", _serialize_tests(tests, session))
        write_json("findings.json", _serialize_findings(findings, session))

        # Evidence snapshots (connector-sourced, no bytes to embed — the
        # normalized summary itself is the exportable fact).
        snapshot_ids = {
            snapshot_id
            for occurrence in occurrences
            for snapshot_id in [
                link.evidence_snapshot_id
                for link in session.scalars(
                    select(ControlOccurrenceEvidence).where(
                        ControlOccurrenceEvidence.occurrence_id == occurrence.id
                    )
                ).all()
            ]
        }
        if snapshot_ids:
            snapshots = session.scalars(
                select(EvidenceSnapshot).where(EvidenceSnapshot.id.in_(snapshot_ids))
            ).all()
            write_json(
                "evidence_snapshots.json",
                [
                    {
                        "id": s.id,
                        "source_type": s.source_type,
                        "check_key": s.check_key,
                        "status": s.status,
                        "title": s.title,
                        "summary": s.summary,
                        "collected_at": _iso(s.collected_at),
                        "raw_payload_sha256": s.raw_payload_sha256,
                    }
                    for s in snapshots
                ],
            )

        # EvidenceArtifactVersion bytes (#32) — from occurrences and tests.
        artifact_version_ids: set[str] = set()
        for occurrence in occurrences:
            artifact_version_ids.update(
                link.evidence_artifact_version_id
                for link in session.scalars(
                    select(ControlOccurrenceEvidenceArtifact).where(
                        ControlOccurrenceEvidenceArtifact.occurrence_id == occurrence.id
                    )
                ).all()
            )
        for test in tests:
            artifact_version_ids.update(
                link.evidence_artifact_version_id
                for link in session.scalars(
                    select(ControlTestEvidenceArtifact).where(ControlTestEvidenceArtifact.test_id == test.id)
                ).all()
            )

        if artifact_version_ids:
            try:
                client = build_s3_client(settings)
            except ObjectStorageNotConfiguredError as exc:
                raise AuditPackageError(
                    "Evidence artifacts are referenced by this period but the evidence "
                    "repository is not configured — cannot verify their bytes."
                ) from exc

            versions = session.scalars(
                select(EvidenceArtifactVersion).where(EvidenceArtifactVersion.id.in_(artifact_version_ids))
            ).all()
            for version in versions:
                try:
                    content = download_object(
                        client, bucket=settings.s3_bucket, object_key=version.object_key
                    )
                except ObjectStorageError as exc:
                    raise AuditPackageError(
                        f"Evidence artifact version '{version.id}' could not be retrieved: {exc}"
                    ) from exc
                actual_sha256 = _sha256(content)
                if actual_sha256 != version.sha256:
                    raise AuditPackageError(
                        f"Evidence artifact version '{version.id}' failed integrity verification "
                        "(stored hash does not match retrieved bytes)."
                    )
                path = f"evidence/{version.artifact_id}/{version.id}-{version.original_filename}"
                archive.writestr(path, content)
                manifest_entries.append({"path": path, "sha256": actual_sha256, "byte_size": len(content)})

        # Policy versions relevant to the period.
        policies = session.scalars(select(Policy)).all()
        policy_control_ids: dict[str, list[str]] = {}
        for mapping in session.scalars(select(PolicyControlMapping)).all():
            policy_control_ids.setdefault(mapping.policy_id, []).append(mapping.control_id)

        policy_summaries = []
        for policy in policies:
            relevant_versions = _policy_versions_for_period(session, policy, start, end)
            for version in relevant_versions:
                file_path = policy_version_path(
                    settings.data_dir, policy.id, version.version_number, version.stored_filename
                )
                if not os.path.isfile(file_path):
                    raise AuditPackageError(
                        f"Policy '{policy.title}' version {version.version_number} is missing "
                        "its stored file."
                    )
                with open(file_path, "rb") as fh:
                    content = fh.read()
                actual_sha256 = _sha256(content)
                if actual_sha256 != version.sha256:
                    raise AuditPackageError(
                        f"Policy '{policy.title}' version {version.version_number} failed integrity "
                        "verification (stored hash does not match the file on disk)."
                    )
                path = f"policies/{policy.id}/version-{version.version_number}-{version.original_filename}"
                archive.writestr(path, content)
                manifest_entries.append({"path": path, "sha256": actual_sha256, "byte_size": len(content)})
            if relevant_versions:
                policy_summaries.append(
                    {
                        "id": policy.id,
                        "title": policy.title,
                        "owner": policy.owner,
                        "mapped_control_ids": policy_control_ids.get(policy.id, []),
                        "versions": [
                            {
                                "version_number": v.version_number,
                                "lifecycle_status": v.lifecycle_status,
                                "effective_at": _iso(v.effective_at),
                                "superseded_at": _iso(v.superseded_at),
                                "sha256": v.sha256,
                            }
                            for v in relevant_versions
                        ],
                    }
                )
        write_json("policies.json", policy_summaries)

        manifest = {
            "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "generated_by": actor_email,
            "audit_period_starts_on": _iso(start),
            "audit_period_ends_on": _iso(end),
            "disclaimer": DISCLAIMER,
            "files": sorted(manifest_entries, key=lambda entry: entry["path"]),
        }
        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        manifest_sha256 = _sha256(manifest_bytes)
        archive.writestr("manifest.json", manifest_bytes)

    return buffer.getvalue(), manifest_sha256
