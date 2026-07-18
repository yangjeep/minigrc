"""AWS connection settings and CloudTrail/IAM evidence collection.

Connection settings and check execution are admin-only (this touches an
account-level trust relationship, even without storing long-lived keys).
Evidence snapshots themselves remain viewable by any authenticated user,
consistent with the rest of this app's GRC data.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.aws_connector import (
    AwsConnectionError,
    build_evidence_snapshot,
    build_session,
    check_cloudtrail,
    check_iam,
    test_connection,
)
from app.crypto import DecryptionError, EncryptionNotConfiguredError, decrypt, encrypt
from app.deps import get_db, require_admin, require_login, verify_csrf
from app.flash import redirect_with_flash
from app.models import AwsConnection

router = APIRouter(prefix="/connectors/aws", tags=["connectors"], dependencies=[Depends(require_login)])


def get_connection(db: Session) -> AwsConnection | None:
    return db.scalar(select(AwsConnection).order_by(AwsConnection.created_at.desc()).limit(1))


def resolve_external_id(connection: AwsConnection, *, encryption_key: str) -> str | None:
    if not connection.encrypted_external_id:
        return None
    try:
        return decrypt(connection.encrypted_external_id, key=encryption_key)
    except (DecryptionError, EncryptionNotConfiguredError) as exc:
        raise AwsConnectionError(
            "The stored AWS external ID could not be decrypted. "
            "Restore the GRC_ENCRYPTION_KEY used when this connection was saved, "
            "or enter and save the external ID again."
        ) from exc


@router.get("")
def view_connection(request: Request, db: Session = Depends(get_db)):
    connection = get_connection(db)
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "connectors/aws.html", {"connection": connection})


@router.get("/edit")
def edit_connection_form(request: Request, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    connection = get_connection(db)
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "connectors/aws_edit.html", {"connection": connection})


@router.post("")
def save_connection(
    request: Request,
    account_label: str = Form(...),
    expected_account_id: str = Form(""),
    role_arn: str = Form(""),
    external_id: str = Form(""),
    regions: str = Form("us-east-1"),
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
    _csrf: None = Depends(verify_csrf),
):
    account_label = account_label.strip()
    if not account_label:
        return redirect_with_flash("/connectors/aws/edit", "Account label is required.", kind="error")

    settings = request.app.state.settings
    connection = get_connection(db)

    encrypted_external_id = None
    if external_id.strip():
        try:
            encrypted_external_id = encrypt(external_id.strip(), key=settings.encryption_key)
        except EncryptionNotConfiguredError as exc:
            return redirect_with_flash("/connectors/aws/edit", str(exc), kind="error")
    elif connection is not None:
        encrypted_external_id = connection.encrypted_external_id  # keep existing unless explicitly changed

    if connection is None:
        connection = AwsConnection(configured_by_user_id=admin.id)
        db.add(connection)

    connection.account_label = account_label
    connection.expected_account_id = expected_account_id.strip()
    connection.role_arn = role_arn.strip() or None
    connection.encrypted_external_id = encrypted_external_id
    connection.regions = regions.strip() or "us-east-1"
    db.flush()

    record_audit_event(
        db,
        entity_type="aws_connection",
        entity_id=connection.id,
        action="save",
        detail=f"Saved AWS connection settings for '{account_label}' (external ID redacted)",
        actor=admin.email,
    )
    return redirect_with_flash("/connectors/aws", "AWS connection settings saved.")


@router.post("/test")
def run_test_connection(
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
    _csrf: None = Depends(verify_csrf),
):
    connection = get_connection(db)
    if connection is None:
        return redirect_with_flash("/connectors/aws", "Configure an AWS connection first.", kind="error")

    settings = request.app.state.settings
    region = connection.regions.split(",")[0].strip() if connection.regions else None

    try:
        external_id = resolve_external_id(connection, encryption_key=settings.encryption_key)
        session = build_session(role_arn=connection.role_arn, external_id=external_id, region=region)
        result = test_connection(session, expected_account_id=connection.expected_account_id)
    except AwsConnectionError as exc:
        connection.last_error_summary = str(exc)
        return redirect_with_flash("/connectors/aws", f"Connection test failed: {exc}", kind="error")

    connection.last_check_at = datetime.datetime.now(datetime.UTC)
    connection.last_error_summary = ""
    record_audit_event(
        db,
        entity_type="aws_connection",
        entity_id=connection.id,
        action="test_connection",
        detail=result.summary,
        actor=admin.email,
    )
    return redirect_with_flash("/connectors/aws", f"Connection test succeeded: {result.summary}")


@router.post("/run-checks")
def run_checks(
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
    _csrf: None = Depends(verify_csrf),
):
    connection = get_connection(db)
    if connection is None:
        return redirect_with_flash("/connectors/aws", "Configure an AWS connection first.", kind="error")

    settings = request.app.state.settings
    region = connection.regions.split(",")[0].strip() if connection.regions else None

    try:
        external_id = resolve_external_id(connection, encryption_key=settings.encryption_key)
        session = build_session(role_arn=connection.role_arn, external_id=external_id, region=region)
    except AwsConnectionError as exc:
        connection.last_error_summary = str(exc)
        return redirect_with_flash("/connectors/aws", f"Could not start an AWS session: {exc}", kind="error")

    results = [check_cloudtrail(session), check_iam(session)]
    for result in results:
        db.add(build_evidence_snapshot(result, connection_id=connection.id))

    connection.last_check_at = datetime.datetime.now(datetime.UTC)
    connection.last_error_summary = ""
    record_audit_event(
        db,
        entity_type="aws_connection",
        entity_id=connection.id,
        action="run_checks",
        detail=f"Ran AWS evidence checks: {', '.join(f'{r.check_key}={r.status}' for r in results)}",
        actor=admin.email,
    )
    return redirect_with_flash("/connectors/aws", "AWS checks complete — see Evidence for results.")
