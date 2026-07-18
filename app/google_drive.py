"""Google Drive read-only connector: OAuth token refresh, file metadata,
revisions, and content download/export via the Drive API v3 over HTTPS.

Distinct from Google OIDC login (app/google_oidc.py) — this always
operates against one shared, admin-managed org-level connection stored in
`GoogleDriveConnection`, never a signed-in user's personal session.
Requests only the read-only `drive.readonly` scope — no write access is
ever requested, matching the least-privilege requirement for this
integration.

Google explicitly warns that revision history may be purged and revision
listings may be incomplete:
https://developers.google.com/workspace/drive/api/guides/manage-revisions
This module never claims otherwise — the locally captured, immutable
`PolicyVersion` is the actual archival record, not Drive's revision list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

# Google Docs/Sheets/Slides have no downloadable native bytes — they must be
# exported. PDF is the deterministic, archival-friendly export format for
# policies specifically (per this branch's spec).
GOOGLE_WORKSPACE_EXPORT_MIME_TYPES = {
    "application/vnd.google-apps.document": "application/pdf",
    "application/vnd.google-apps.spreadsheet": "application/pdf",
    "application/vnd.google-apps.presentation": "application/pdf",
}

_DRIVE_URL_ID_RE = re.compile(r"/d/([a-zA-Z0-9_-]{10,})")
_DRIVE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{10,}$")


class GoogleDriveError(ValueError):
    """User-facing reason a Drive operation was rejected or failed."""


def parse_drive_file_id(value: str) -> str:
    """Extract and validate a Drive file ID from a raw ID or a Drive/Docs URL.

    Never used to fetch the given value as a URL — only to build our own
    Google API request from a validated ID. Prevents SSRF via a
    user-supplied "Drive URL" that actually points somewhere else.
    """
    value = value.strip()
    match = _DRIVE_URL_ID_RE.search(value)
    candidate = match.group(1) if match else value
    if not _DRIVE_ID_RE.match(candidate):
        raise GoogleDriveError("Not a recognized Google Drive file ID or URL.")
    return candidate


def build_authorization_url(*, client_id: str, redirect_uri: str, state: str, extra_scopes: str = "") -> str:
    """`extra_scopes` (space-separated) lets the optional Workspace
    Directory sync request its read-only scope in the same consent grant
    as this connection, without making Directory sync a separate OAuth
    flow — see app/google_workspace_directory.py."""
    scope = f"{DRIVE_SCOPE} {extra_scopes}".strip()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTHORIZATION_ENDPOINT}?{urlencode(params)}"


def exchange_code_for_tokens(
    *, code: str, client_id: str, client_secret: str, redirect_uri: str, timeout: float = 10.0
) -> dict:
    """Exchange an authorization code for tokens. Requires `refresh_token`
    in the response — callers should ask the user to revoke prior consent
    at https://myaccount.google.com/permissions and reconnect if Google
    omits it (happens when consent was already granted without `prompt=consent`).
    """
    try:
        response = httpx.post(
            TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=timeout,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise GoogleDriveError("Could not reach Google to complete the Drive connection.") from exc

    payload = response.json()
    if not payload.get("refresh_token"):
        raise GoogleDriveError(
            "Google did not return a refresh token. Revoke this app's prior access at "
            "https://myaccount.google.com/permissions and try connecting again."
        )
    return payload


def get_access_token(*, refresh_token: str, client_id: str, client_secret: str) -> str:
    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_ENDPOINT,
        client_id=client_id,
        client_secret=client_secret,
    )
    try:
        credentials.refresh(GoogleAuthRequest())
    except Exception as exc:
        raise GoogleDriveError(
            "Could not refresh the Google Drive connection's access token. "
            "The connection may have been revoked and should be reconnected."
        ) from exc
    return credentials.token


def revoke_token(token: str, *, timeout: float = 10.0) -> None:
    """Best-effort revoke at Google — disconnect proceeds either way."""
    try:
        httpx.post(REVOKE_ENDPOINT, params={"token": token}, timeout=timeout)
    except httpx.HTTPError:
        pass


@dataclass(frozen=True)
class DriveFileMetadata:
    file_id: str
    name: str
    mime_type: str
    web_view_link: str | None
    current_revision_id: str | None


def _raise_for_drive_status(exc: httpx.HTTPStatusError) -> None:
    status = exc.response.status_code
    if status == 404:
        raise GoogleDriveError("Drive file not found or not accessible with the current connection.") from exc
    if status == 403:
        raise GoogleDriveError(
            "Drive denied access to this file with the current connection's permissions."
        ) from exc
    raise GoogleDriveError(f"Google Drive returned an unexpected error ({status}).") from exc


def get_file_metadata(file_id: str, *, access_token: str, timeout: float = 10.0) -> DriveFileMetadata:
    try:
        response = httpx.get(
            f"{DRIVE_API_BASE}/files/{file_id}",
            params={"fields": "id,name,mimeType,webViewLink,headRevisionId", "supportsAllDrives": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _raise_for_drive_status(exc)
    except httpx.HTTPError as exc:
        raise GoogleDriveError("Could not reach Google Drive.") from exc

    payload = response.json()
    return DriveFileMetadata(
        file_id=payload["id"],
        name=payload.get("name", ""),
        mime_type=payload.get("mimeType", ""),
        web_view_link=payload.get("webViewLink"),
        current_revision_id=payload.get("headRevisionId"),
    )


def list_revisions(file_id: str, *, access_token: str, timeout: float = 10.0) -> list[dict]:
    """Best-effort revision metadata — never treated as complete/permanent history."""
    try:
        response = httpx.get(
            f"{DRIVE_API_BASE}/files/{file_id}/revisions",
            params={"fields": "revisions(id,modifiedTime,mimeType)"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return []
    return response.json().get("revisions", [])


def download_file_content(
    metadata: DriveFileMetadata, *, access_token: str, max_bytes: int, timeout: float = 30.0
) -> bytes:
    """Download or export content while enforcing the upload limit as it streams."""
    export_mime_type = GOOGLE_WORKSPACE_EXPORT_MIME_TYPES.get(metadata.mime_type)
    url = f"{DRIVE_API_BASE}/files/{metadata.file_id}"
    params = (
        {"mimeType": export_mime_type} if export_mime_type else {"alt": "media", "supportsAllDrives": "true"}
    )
    if export_mime_type:
        url += "/export"

    try:
        with httpx.stream(
            "GET",
            url,
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
        ) as response:
            response.raise_for_status()

            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    declared_size = None
                if declared_size is not None and declared_size > max_bytes:
                    raise GoogleDriveError(
                        f"Drive file exceeds the configured {max_bytes // (1024 * 1024)} MB upload limit."
                    )

            chunks: list[bytes] = []
            total_bytes = 0
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise GoogleDriveError(
                        f"Drive file exceeds the configured {max_bytes // (1024 * 1024)} MB upload limit."
                    )
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPStatusError as exc:
        _raise_for_drive_status(exc)
    except httpx.HTTPError as exc:
        raise GoogleDriveError("Could not download the Drive file's content.") from exc


BLOB_MIME_TYPE_EXTENSIONS = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}


def captured_filename(metadata: DriveFileMetadata) -> str:
    """A display filename for the captured content, guaranteed to carry the
    extension `app/storage.py` validates on — Drive's own `name` field
    doesn't always include one (a user can name a PDF "Security Policy"
    with no `.pdf` suffix)."""
    if metadata.mime_type in GOOGLE_WORKSPACE_EXPORT_MIME_TYPES:
        return metadata.name if metadata.name.lower().endswith(".pdf") else f"{metadata.name}.pdf"

    extension = BLOB_MIME_TYPE_EXTENSIONS.get(metadata.mime_type)
    if extension and not metadata.name.lower().endswith(f".{extension}"):
        return f"{metadata.name}.{extension}"
    return metadata.name
