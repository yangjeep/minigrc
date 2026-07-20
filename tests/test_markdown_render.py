"""Tests for the shared safe-Markdown renderer (Feature 11/12).

Content rendered here can originate from admin-authored Trust Center
copy today and will be shown to unauthenticated visitors once the
public route (Feature 12) exists — treated as untrusted at render time
regardless of who authored it, matching the app's general posture
toward imported/user-supplied content (see app/imports.py).
"""

from __future__ import annotations

from app.markdown_render import render_markdown_safe


def test_renders_basic_markdown_to_html():
    html = render_markdown_safe("# Title\n\nSome **bold** text.")
    assert "<h1>Title</h1>" in html
    assert "<strong>bold</strong>" in html


def test_strips_script_tags():
    html = render_markdown_safe("Hello <script>alert('xss')</script> world")
    assert "<script" not in html
    assert "alert(" not in html


def test_strips_inline_event_handlers():
    html = render_markdown_safe('<img src="x" onerror="alert(1)">')
    assert "onerror" not in html


def test_strips_javascript_uri_links():
    html = render_markdown_safe("[click me](javascript:alert(1))")
    assert "javascript:" not in html


def test_allows_safe_links_and_lists():
    html = render_markdown_safe("- one\n- two\n\n[docs](https://example.com)")
    assert "<li>one</li>" in html
    assert '<a href="https://example.com" rel="noopener noreferrer">docs</a>' in html


def test_empty_input_renders_empty_string():
    assert render_markdown_safe("") == ""
    assert render_markdown_safe(None) == ""


def test_heading_offset_shifts_heading_levels():
    html = render_markdown_safe("# Title\n\n## Subtitle", heading_offset=2)
    assert "<h3>Title</h3>" in html
    assert "<h4>Subtitle</h4>" in html


def test_heading_offset_clamps_at_h6():
    html = render_markdown_safe("##### Deep\n\n###### Deeper", heading_offset=2)
    assert "<h6>Deep</h6>" in html
    assert "<h6>Deeper</h6>" in html


def test_default_heading_offset_is_zero():
    html = render_markdown_safe("# Title")
    assert "<h1>Title</h1>" in html
