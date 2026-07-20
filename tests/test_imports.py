"""Tests for the native import subsystem (Feature 8).

See the architecture checkpoint on umbrella issue #5 and the import
threat model there (CSV-injection guarding, checksum idempotency, bounded
reads, all-or-nothing execution).
"""

from __future__ import annotations

from app.imports import (
    compute_checksum,
    neutralize_csv_formula,
    run_import,
)
from app.models import Framework, ImportJob, Risk

FRAMEWORK_CSV = b"reference_code,title,description,display_order\nX.1,Imported req,desc,1\n"

RISK_CSV = (
    b"title,description,category,likelihood,impact,owner,status\nVendor breach,desc,security,3,4,me,open\n"
)

RISK_CSV_INJECTION = (
    b"title,description,category,likelihood,impact,owner,status\n"
    b"\"=cmd|'/c calc'!A1\",desc,security,3,4,me,open\n"
)

RISK_CSV_ROW_ERROR = (
    b"title,description,category,likelihood,impact,owner,status\n"
    b"Good row,desc,security,3,4,me,open\n"
    b",desc,security,3,4,me,open\n"  # blank title
)


def test_neutralize_csv_formula_prefixes_dangerous_leading_chars():
    assert neutralize_csv_formula("=cmd|'/c calc'!A1").startswith("'=")
    assert neutralize_csv_formula("+1+1").startswith("'+")
    assert neutralize_csv_formula("-1-1").startswith("'-")
    assert neutralize_csv_formula("@SUM(A1)").startswith("'@")
    assert neutralize_csv_formula("ordinary text") == "ordinary text"


def test_compute_checksum_is_stable_and_content_sensitive():
    a = compute_checksum(b"hello")
    b = compute_checksum(b"hello")
    c = compute_checksum(b"world")
    assert a == b
    assert a != c


def test_risk_csv_import_creates_rows_and_neutralizes_injection(app):
    with app.state.session_factory() as session:
        job = run_import(
            session,
            importer_name="risk_register_csv",
            raw_bytes=RISK_CSV_INJECTION,
            filename="risks.csv",
            target={},
            actor="admin@example.com",
            source="web",
        )
        session.commit()
        assert job.status == "completed"
        assert job.records_created == 1

        risk = session.query(Risk).filter(Risk.category == "security").one()
        assert risk.title.startswith("'=")  # neutralized, not a live formula


def test_risk_csv_import_records_counts_on_import_job(app):
    with app.state.session_factory() as session:
        job = run_import(
            session,
            importer_name="risk_register_csv",
            raw_bytes=RISK_CSV,
            filename="risks.csv",
            target={},
            actor="admin@example.com",
            source="web",
        )
        session.commit()
        assert job.records_discovered == 1
        assert job.records_created == 1
        assert job.checksum_sha256 == compute_checksum(RISK_CSV)
        assert job.original_filename == "risks.csv"


def test_risk_csv_import_rejects_and_rolls_back_on_row_error(app):
    with app.state.session_factory() as session:
        job = run_import(
            session,
            importer_name="risk_register_csv",
            raw_bytes=RISK_CSV_ROW_ERROR,
            filename="risks.csv",
            target={},
            actor="admin@example.com",
            source="web",
        )
        session.commit()
        assert job.status == "rejected"
        assert job.records_created == 0
        assert job.validation_errors_json is not None

    with app.state.session_factory() as session:
        assert session.query(Risk).filter(Risk.title == "Good row").count() == 0  # nothing written


def test_duplicate_import_is_idempotent_via_checksum(app):
    with app.state.session_factory() as session:
        first = run_import(
            session,
            importer_name="risk_register_csv",
            raw_bytes=RISK_CSV,
            filename="risks.csv",
            target={},
            actor="admin@example.com",
            source="web",
        )
        session.commit()
        assert first.status == "completed"

    with app.state.session_factory() as session:
        second = run_import(
            session,
            importer_name="risk_register_csv",
            raw_bytes=RISK_CSV,
            filename="risks-again.csv",
            target={},
            actor="admin@example.com",
            source="web",
        )
        session.commit()
        assert second.status == "completed"
        assert second.records_created == 0  # skipped — duplicate of the first job
        assert "duplicate" in (second.validation_errors_json or "").lower() or second.records_skipped == 1

    with app.state.session_factory() as session:
        assert session.query(Risk).filter(Risk.category == "security").count() == 1  # not imported twice


def test_malformed_encoding_rejected_cleanly(app):
    with app.state.session_factory() as session:
        job = run_import(
            session,
            importer_name="risk_register_csv",
            raw_bytes=b"\xff\xfe not valid utf-8 \x80\x81",
            filename="risks.csv",
            target={},
            actor="admin@example.com",
            source="web",
        )
        session.commit()
        assert job.status == "rejected"


def test_oversize_file_rejected(app):
    with app.state.session_factory() as session:
        big = b"title,description,category,likelihood,impact,owner,status\n" + (b"a" * 30_000_000)
        job = run_import(
            session,
            importer_name="risk_register_csv",
            raw_bytes=big,
            filename="risks.csv",
            target={},
            actor="admin@example.com",
            source="web",
            max_bytes=1_000_000,
        )
        session.commit()
        assert job.status == "rejected"
        assert (
            "size" in (job.validation_errors_json or "").lower()
            or "large" in (job.validation_errors_json or "").lower()
        )


def test_unsupported_importer_name_fails_gracefully(app):
    with app.state.session_factory() as session:
        job = run_import(
            session,
            importer_name="no_such_importer",
            raw_bytes=b"x",
            filename="x.csv",
            target={},
            actor="admin@example.com",
            source="web",
        )
        session.commit()
        assert job.status == "rejected"


def test_framework_requirements_csv_importer_reuses_existing_logic(app):
    with app.state.session_factory() as session:
        framework = Framework(name="Import Target", version="1.0", description="")
        session.add(framework)
        session.flush()
        framework_id = framework.id
        session.commit()

    with app.state.session_factory() as session:
        job = run_import(
            session,
            importer_name="framework_requirements_csv",
            raw_bytes=FRAMEWORK_CSV,
            filename="reqs.csv",
            target={"framework_id": framework_id},
            actor="admin@example.com",
            source="web",
        )
        session.commit()
        assert job.status == "completed"
        assert job.records_created == 1


def test_import_job_persists_source_and_actor(app):
    with app.state.session_factory() as session:
        job = run_import(
            session,
            importer_name="risk_register_csv",
            raw_bytes=RISK_CSV,
            filename="risks.csv",
            target={},
            actor="admin@example.com",
            source="cli",
        )
        session.commit()
        job_id = job.id

    with app.state.session_factory() as session:
        job = session.get(ImportJob, job_id)
        assert job.source == "cli"
        assert job.created_by == "admin@example.com"
        assert job.started_at is not None
        assert job.completed_at is not None
