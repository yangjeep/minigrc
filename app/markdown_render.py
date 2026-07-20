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

import re

import markdown as _markdown
import nh3

_HEADING_TAG_RE = re.compile(r"<(/?)h([1-6])([ >])")

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


def _shift_headings(html: str, offset: int) -> str:
    """Shift h1-h6 tags down by `offset` levels, clamped at h6.

    Admin-authored Markdown is written with no knowledge of where it will
    be embedded (e.g. under a page `<h1>` and a per-section `<h2>`) — an
    unclamped `# Heading` would produce a second, competing top-level
    heading for screen-reader users navigating by heading outline. See
    tests/test_markdown_render.py.
    """
    if offset <= 0:
        return html

    def repl(match: re.Match[str]) -> str:
        slash, level, sep = match.group(1), int(match.group(2)), match.group(3)
        return f"<{slash}h{min(level + offset, 6)}{sep}"

    return _HEADING_TAG_RE.sub(repl, html)


def render_markdown_safe(text: str | None, *, heading_offset: int = 0) -> str:
    if not text:
        return ""
    html = _markdown.markdown(text, extensions=["extra", "sane_lists"])
    cleaned = nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer",
    )
    return _shift_headings(cleaned, heading_offset)
