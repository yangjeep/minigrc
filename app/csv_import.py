"""CSV import for framework requirements.

Expected columns: reference_code, title, description (optional),
display_order (optional). Validates every row before writing anything —
either the whole file imports or none of it does, so a malformed file never
leaves a framework half-imported.
"""

from __future__ import annotations

import csv
import io

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.models import Framework
from app.requirements import add_requirement

REQUIRED_COLUMNS = {"reference_code", "title"}

_CHUNK_SIZE = 1024 * 1024


class CsvTooLargeError(ValueError):
    """Raised when a CSV upload exceeds the configured size limit."""


def read_csv_upload(upload: UploadFile, *, max_bytes: int) -> bytes:
    """Read an uploaded CSV in bounded chunks, never buffering past max_bytes.

    Raises CsvTooLargeError before the full file is read into memory, so an
    oversized upload never reaches the parser or the database.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := upload.file.read(_CHUNK_SIZE):
        total += len(chunk)
        if total > max_bytes:
            raise CsvTooLargeError(
                f"CSV file exceeds the maximum upload size of {max_bytes // (1024 * 1024)} MB."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def import_requirements_csv(
    session: Session, framework: Framework, raw_bytes: bytes
) -> tuple[int, list[str]]:
    """Returns (rows_created, errors). If errors is non-empty, nothing was written."""
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return 0, ["File is not valid UTF-8 text."]

    reader = csv.DictReader(io.StringIO(text))
    fieldnames = set(reader.fieldnames or [])
    missing = REQUIRED_COLUMNS - fieldnames
    if missing:
        return 0, [f"Missing required column(s): {', '.join(sorted(missing))}"]

    existing_codes = {r.reference_code for r in framework.requirements}
    seen_in_file: set[str] = set()
    errors: list[str] = []
    parsed: list[tuple[str, str, str, int]] = []

    for line_number, row in enumerate(reader, start=2):
        code = (row.get("reference_code") or "").strip()
        title = (row.get("title") or "").strip()
        description = (row.get("description") or "").strip()
        display_order_raw = (row.get("display_order") or "").strip()

        if not code:
            errors.append(f"Row {line_number}: reference_code is required")
            continue
        if not title:
            errors.append(f"Row {line_number}: title is required")
            continue
        if code in existing_codes:
            errors.append(f"Row {line_number}: reference_code '{code}' already exists in this framework")
            continue
        if code in seen_in_file:
            errors.append(f"Row {line_number}: duplicate reference_code '{code}' within the file")
            continue

        display_order = line_number - 1
        if display_order_raw:
            try:
                display_order = int(display_order_raw)
            except ValueError:
                errors.append(f"Row {line_number}: display_order must be an integer")
                continue

        seen_in_file.add(code)
        parsed.append((code, title, description, display_order))

    if errors:
        return 0, errors
    if not parsed:
        return 0, ["CSV file contained no data rows."]

    for code, title, description, display_order in parsed:
        add_requirement(
            session,
            framework,
            reference_code=code,
            title=title,
            summary=description,
            display_order=display_order,
        )

    return len(parsed), []
