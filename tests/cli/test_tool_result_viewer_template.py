"""Tier 2: TemplateSchema + _apply_template safety (#1154 Phase 3 S2).

Pins the TemplateSchema data structure and _apply_template renderer:
- rows render as label/value table pairs.
- missing fields are skipped silently (no KeyError).
- values containing Rich markup are escaped (untrusted external content,
  same threat surface as #1822).
- shape_fingerprint produces a stable frozenset key.
- _SHAPE_TEMPLATE_CACHE is keyed by fingerprint (S3 will populate it).

Falsification:
- Without escape(val), a value containing "[bold]x[/bold]" would inject
  Rich markup into the TUI table; the plain-text assertion would fail.
- Without field validation at apply time, a missing field would raise
  KeyError instead of silently skipping the row.
"""
from __future__ import annotations

from rich.text import Text

from reyn.interfaces.tui.widgets.right_panel.tool_result_viewers import (
    _SHAPE_TEMPLATE_CACHE,
    TemplateSchema,
    _apply_template,
    _shape_fingerprint,
)


def _plain(renderable) -> str:
    """Extract plain text from a Rich renderable via console render."""
    from io import StringIO

    from rich.console import Console

    buf = StringIO()
    console = Console(file=buf, highlight=False, markup=True, width=120)
    console.print(renderable)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _apply_template — rendering
# ---------------------------------------------------------------------------

def test_apply_template_renders_label_value_pairs() -> None:
    """Tier 2: _apply_template produces output containing label and value strings.

    Falsification: if rows were never rendered, neither the label nor the
    value would appear in the output.
    """
    schema = TemplateSchema(
        rows=[("From", "sender"), ("Subject", "subject")],
        caption="email result",
    )
    result = {"sender": "alice@example.com", "subject": "Hello"}
    plain = _plain(_apply_template(result, schema))
    assert "From" in plain, f"expected label 'From' in output: {plain!r}"
    assert "alice@example.com" in plain, f"expected value in output: {plain!r}"
    assert "Subject" in plain
    assert "Hello" in plain


def test_apply_template_skips_missing_field() -> None:
    """Tier 2: a field named in schema but absent in result is silently skipped.

    Falsification: without the ``if val is None: continue`` guard,
    _apply_template would raise KeyError on a missing field.
    """
    schema = TemplateSchema(
        rows=[("Present", "exists"), ("Missing", "absent")],
        caption="",
    )
    result = {"exists": "here"}
    # Should not raise; "Missing" row is omitted.
    plain = _plain(_apply_template(result, schema))
    assert "Present" in plain
    assert "here" in plain
    assert "Missing" not in plain, f"expected missing field row to be skipped: {plain!r}"


def test_apply_template_escapes_value_markup() -> None:
    """Tier 2: result values containing Rich markup are escaped before display.

    Tool-result values are untrusted external content (#1822 threat surface).
    A value containing "[bold]injected[/bold]" must render as literal text,
    not as a Rich markup instruction.

    Falsification: without escape() on the value, Rich would interpret the
    markup tags and the plain text would not contain the literal bracket
    characters.
    """
    schema = TemplateSchema(rows=[("Data", "payload")], caption="")
    result = {"payload": "[bold]injected[/bold]"}
    plain = _plain(_apply_template(result, schema))
    assert "[bold]" in plain, (
        f"expected literal '[bold]' in escaped output, got: {plain!r}"
    )
    assert "injected" in plain


def test_apply_template_escapes_label_markup() -> None:
    """Tier 2: pre-escaped labels (from S3 schema construction) render as literal text.

    Labels arrive pre-escaped at _apply_template; this test confirms the
    pre-escaped form is passed through correctly (the escape() call is at
    schema construction time in S3, not at apply time for labels).
    """
    from rich.markup import escape
    raw_label = "[red]danger[/red]"
    schema = TemplateSchema(rows=[(escape(raw_label), "field")], caption="")
    result = {"field": "value"}
    plain = _plain(_apply_template(result, schema))
    assert "[red]" in plain, (
        f"expected literal '[red]' in label output (pre-escaped), got: {plain!r}"
    )


def test_apply_template_caps_value_length() -> None:
    """Tier 2: values longer than 500 chars are truncated before display.

    Falsification: without the [:500] cap, a 10KB base64 blob would render
    in full, making the preview pane unreadable.
    """
    schema = TemplateSchema(rows=[("Big", "data")], caption="")
    long_val = "x" * 2000
    result = {"data": long_val}
    plain = _plain(_apply_template(result, schema))
    # The rendered value must not contain 2000 x's (capped at 500).
    assert "x" * 501 not in plain, "expected value to be truncated at 500 chars"
    assert "x" * 10 in plain, "expected some of the value to appear"


def test_apply_template_caption_set_on_table() -> None:
    """Tier 2: schema caption is assigned to the returned Rich Table.

    Checks behavior (caption wiring) without pinning Rich's word-wrap layout,
    which varies with table width.

    Falsification: if _apply_template never assigned schema.caption, the
    table.caption attribute would be the Rich default (empty string / None).
    """
    from rich.table import Table
    schema = TemplateSchema(rows=[("Key", "k")], caption="email result")
    result = {"k": "v"}
    table = _apply_template(result, schema)
    assert isinstance(table, Table)
    assert table.caption == "email result", (
        f"expected caption 'email result' on table, got {table.caption!r}"
    )


# ---------------------------------------------------------------------------
# _shape_fingerprint
# ---------------------------------------------------------------------------

def test_shape_fingerprint_is_frozenset_of_keys() -> None:
    """Tier 2: _shape_fingerprint returns frozenset of top-level keys."""
    result = {"a": 1, "b": 2, "c": 3}
    fp = _shape_fingerprint(result)
    assert fp == frozenset({"a", "b", "c"}), f"unexpected fingerprint: {fp!r}"


def test_shape_fingerprint_same_keys_same_fp() -> None:
    """Tier 2: two results with the same keys produce the same fingerprint."""
    r1 = {"sender": "x", "subject": "y"}
    r2 = {"sender": "different", "subject": "also different"}
    assert _shape_fingerprint(r1) == _shape_fingerprint(r2)


def test_shape_fingerprint_different_keys_different_fp() -> None:
    """Tier 2: results with different keys produce different fingerprints."""
    r1 = {"a": 1, "b": 2}
    r2 = {"a": 1, "c": 2}
    assert _shape_fingerprint(r1) != _shape_fingerprint(r2)


# ---------------------------------------------------------------------------
# _SHAPE_TEMPLATE_CACHE — structure check
# ---------------------------------------------------------------------------

def test_shape_template_cache_accepts_schema_and_none() -> None:
    """Tier 2: _SHAPE_TEMPLATE_CACHE can store TemplateSchema and None values.

    The cache maps frozenset[str] → TemplateSchema | None. None signals
    "generation failed; do not retry". This test confirms the dict accepts
    both shapes without type errors.

    Falsification: if the cache rejected None values (e.g. via defaultdict
    that never stores None), the "do not retry" sentinel pattern would break.
    """
    fp = frozenset({"_s2_test_key"})
    schema = TemplateSchema(rows=[("A", "a")], caption="")

    original = _SHAPE_TEMPLATE_CACHE.get(fp, "absent")
    try:
        _SHAPE_TEMPLATE_CACHE[fp] = schema
        assert _SHAPE_TEMPLATE_CACHE[fp] is schema

        _SHAPE_TEMPLATE_CACHE[fp] = None
        assert _SHAPE_TEMPLATE_CACHE[fp] is None
    finally:
        if original == "absent":
            _SHAPE_TEMPLATE_CACHE.pop(fp, None)
        else:
            _SHAPE_TEMPLATE_CACHE[fp] = original
