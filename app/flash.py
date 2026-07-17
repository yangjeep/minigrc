"""Minimal one-shot flash messages, carried in the redirect query string.

No server-side session/flash storage: this app already redirects (303)
after every POST, so appending `?flash=...&flash_kind=...` to that redirect
URL and reading it back on the following GET is simpler than wiring up
signed cookies or a session store for a single string. Messages are never
persisted and never appear outside the one redirected request.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi.responses import RedirectResponse


def redirect_with_flash(
    url: str, message: str, kind: str = "success", status_code: int = 303
) -> RedirectResponse:
    separator = "&" if "?" in url else "?"
    query = urlencode({"flash": message, "flash_kind": kind})
    return RedirectResponse(url=f"{url}{separator}{query}", status_code=status_code)
