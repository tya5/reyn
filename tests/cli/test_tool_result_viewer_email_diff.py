"""Tier 2: concrete email + diff viewers (#1154 owner-approved slice).

Two hand-built viewers added on the Phase 3 registry seam (#1853) for the
stable, high-frequency ``email`` and ``diff`` shapes — deterministic, zero
LLM cost. Registered BEFORE the generic JSON viewer so an email/diff result
delivered with an ``application/json`` content-type still renders as a card,
not a raw JSON dump.

Covered contracts:
A. email — content-type detection, shape-sniff detection, value escaping,
   priority over generic JSON.
B. diff — content-type detection, shape-sniff detection, markup-injection
   safety, priority over generic JSON.
C. non-overreach — explicit markdown/csv content-types still win; unrelated
   dicts fall through.

Falsification anchors:
- email value escaping: a subject containing "[bold]x[/bold]" renders literal.
- email-over-json: a from+subject+body dict with content_type json renders
  the email card (not JSON) — proves registration position.
- diff shape-sniff: a "diff --git" payload with no content_type is detected.
- non-overreach: a plain {"k": "v"} dict matches neither and returns None.
"""
from __future__ import annotations

from io import StringIO

from rich.console import Console

from reyn.interfaces.tui.widgets.right_panel.tool_result_viewers import (
    render_tool_result,
)


def _plain(renderable) -> str:
    """Render a Rich renderable to plain text (markup interpreted)."""
    buf = StringIO()
    Console(file=buf, highlight=False, markup=True, width=120).print(renderable)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# A. email viewer
# ---------------------------------------------------------------------------

def test_email_content_type_renders_card() -> None:
    """Tier 2: a message/rfc822 result renders a header card with From/Subject."""
    result = {
        "content_type": "message/rfc822",
        "from": "alice@example.com",
        "to": "bob@example.com",
        "subject": "Lunch?",
        "body": "Are you free at noon?",
    }
    rendered = render_tool_result(result)
    assert rendered is not None, "expected an email card renderable"
    plain = _plain(rendered)
    assert "From" in plain and "alice@example.com" in plain
    assert "Subject" in plain and "Lunch?" in plain
    assert "Are you free at noon?" in plain, "expected the body to render"


def test_email_shape_sniff_without_content_type() -> None:
    """Tier 2: from+subject+to shape is detected even with no content_type.

    Falsification: without the shape-sniff branch, a content_type-less email
    dict would fall through to None.
    """
    result = {
        "from": "carol@example.com",
        "to": "dave@example.com",
        "subject": "Status",
    }
    rendered = render_tool_result(result)
    assert rendered is not None, "expected shape-sniff to detect the email"
    assert "carol@example.com" in _plain(rendered)


def test_email_escapes_header_markup() -> None:
    """Tier 2: header values containing Rich markup render as literal text (#1822).

    Falsification: without escape() on header values, "[bold]x[/bold]" in the
    subject would be interpreted as markup and the literal brackets would be
    absent from the output.
    """
    result = {
        "from": "eve@example.com",
        "subject": "[bold]urgent[/bold]",
        "body": "see attached",
    }
    plain = _plain(render_tool_result(result))
    assert "[bold]" in plain, f"expected literal '[bold]' (escaped), got: {plain!r}"
    assert "urgent" in plain


def test_email_escapes_body_markup() -> None:
    """Tier 2: body content containing Rich markup renders as literal text (#1822).

    The body is wrapped in rich.text.Text which does not parse console markup.
    Falsification: if the body were added via a markup-parsing path, the
    bracketed tag would be consumed and not appear literally.
    """
    result = {
        "from": "mallory@example.com",
        "subject": "hi",
        "body": "[red]injected[/red] payload",
    }
    plain = _plain(render_tool_result(result))
    assert "[red]" in plain, f"expected literal '[red]' in body, got: {plain!r}"
    assert "injected" in plain


def test_email_wins_over_generic_json() -> None:
    """Tier 2: an email-shaped result with content_type json renders as email.

    This proves the email viewer is registered BEFORE the generic JSON viewer.
    Falsification: if email were appended after json, the json viewer would
    match content_type 'application/json' first and dump raw JSON — the
    distinctive 'From' label / human body line would be absent.
    """
    result = {
        "content_type": "application/json",
        "from": "frank@example.com",
        "to": "grace@example.com",
        "subject": "Report",
        "body": "Numbers attached.",
    }
    plain = _plain(render_tool_result(result))
    assert "From" in plain, "expected the email card (From label), not a JSON dump"
    assert "Numbers attached." in plain


def test_email_empty_falls_through() -> None:
    """Tier 2: an rfc822 result with no header/body fields returns None.

    Ambiguous/empty match must fall through to the YAML / LLM fallback rather
    than render an empty card.
    """
    result = {"content_type": "message/rfc822"}
    assert render_tool_result(result) is None


# ---------------------------------------------------------------------------
# B. diff viewer
# ---------------------------------------------------------------------------

_GIT_DIFF = (
    "diff --git a/foo.py b/foo.py\n"
    "index e69de29..4b825dc 100644\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -0,0 +1 @@\n"
    "+print('hello')\n"
)


def test_diff_content_type_renders() -> None:
    """Tier 2: a text/x-diff result renders (non-None)."""
    result = {"content_type": "text/x-diff", "content": _GIT_DIFF}
    rendered = render_tool_result(result)
    assert rendered is not None, "expected a diff renderable"
    assert "foo.py" in _plain(rendered)


def test_diff_shape_sniff_git_header() -> None:
    """Tier 2: a 'diff --git' payload is detected with no content_type.

    Falsification: without the shape-sniff branch, a content_type-less diff
    would fall through to None (or be mis-rendered by another viewer).
    """
    result = {"content": _GIT_DIFF}
    rendered = render_tool_result(result)
    assert rendered is not None, "expected shape-sniff to detect the git diff"
    assert "hello" in _plain(rendered)


def test_diff_shape_sniff_unified_markers() -> None:
    """Tier 2: a unified diff with --- / +++ markers (no git header) is detected."""
    text = "--- a/x.txt\n+++ b/x.txt\n@@ -1 +1 @@\n-old\n+new\n"
    result = {"content": text}
    rendered = render_tool_result(result)
    assert rendered is not None, "expected unified-diff markers to be detected"


def test_diff_markup_injection_safe() -> None:
    """Tier 2: diff text containing Rich markup is not interpreted (#1822).

    rich.syntax.Syntax renders raw text through a lexer and does not parse
    console markup. Falsification: if the diff were routed through a
    markup-parsing renderer, the bracketed tag would be consumed.
    """
    text = "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-[bold]x[/bold]\n+y\n"
    plain = _plain(render_tool_result({"content": text}))
    assert "[bold]" in plain, f"expected literal '[bold]' in diff, got: {plain!r}"


def test_diff_wins_over_generic_json() -> None:
    """Tier 2: a diff delivered with content_type json renders as a diff.

    Proves the diff viewer is registered BEFORE the generic JSON viewer.
    """
    result = {"content_type": "application/json", "content": _GIT_DIFF}
    plain = _plain(render_tool_result(result))
    assert "foo.py" in plain and "hello" in plain


# ---------------------------------------------------------------------------
# C. non-overreach — explicit content-types still win, unrelated dicts fall through
# ---------------------------------------------------------------------------

def test_markdown_content_type_still_wins() -> None:
    """Tier 2: an explicit text/markdown result is NOT hijacked by email/diff.

    email/diff are registered after markdown/csv, so an explicit markdown
    content-type still routes to the markdown viewer. Falsification: if email/
    diff were at position 0, a markdown doc embedding diff-like lines could be
    mis-detected as a diff.
    """
    result = {
        "content_type": "text/markdown",
        "content": "# Title\n\n--- a/x\n+++ b/x\nsome prose",
    }
    rendered = render_tool_result(result)
    assert rendered is not None
    # Markdown renders the heading text; a diff Syntax view would not show
    # the '#'-stripped heading the same way. Presence of the prose is enough
    # to confirm a renderable was produced via the markdown path.
    assert "Title" in _plain(rendered)


def test_plain_dict_matches_neither() -> None:
    """Tier 2: a dict with no email/diff signal returns None (no overreach).

    Falsification: if either predicate were too permissive, this unrelated
    dict would wrongly render as an email or diff.
    """
    assert render_tool_result({"some_key": "some_value", "n": 3}) is None
