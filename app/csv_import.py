"""CSV import for framework requirements.

Expected columns: reference_code, title, description (optional),
display_order (optional). Validates every row before writing anything —
either the whole file imports or none of it does, so a malformed file never
leaves a framework half-imported.
"""

from __future__ import annotations

import csv
import io

from sqlalchemy.orm import Session

from app.models import Framework
from app.requirements import add_requirement
from app.uploads import UploadTooLargeError as CsvTooLargeError
from app.uploads import read_upload_bounded as read_csv_upload

__all__ = [
    "CsvTooLargeError",
    "read_csv_upload",
    "REQUIRED_COLUMNS",
    "import_requirements_csv",
]

REQUIRED_COLUMNS = {"reference_code", "title"}


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
