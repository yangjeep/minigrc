"""Shared safe-Markdown-to-HTML renderer.

Used by the Trust Center admin preview (Feature 11) and will back the
public route (Feature 12). Content is treated as untrusted regardless
of authorship — `markdown` converts to HTML, `nh3` (Rust/ammonia
bindings) then strips anything outside an explicit allowlist. No
custom sanitization logic is written here; both libraries are
maintained, widely used, and chosen instead of inventing HTML parsing
or a regex-based sanitizer.
"""

from __future__ import annotations

import markdown as _markdown
import nh3

_ALLOWED_TAGS = {
    "p",
    "br",
    "hr",
    "strong",
    "em",
    "b",
    "i",
    "u",
    "s",
    "code",
    "pre",
    "blockquote",
    "ul",
    "ol",
    "li",
    "a",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
}

_ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
}

_ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}


def render_markdown_safe(text: str | None) -> str:
    if not text:
        return ""
    html = _markdown.markdown(text, extensions=["extra", "sane_lists"])
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer",
    )
