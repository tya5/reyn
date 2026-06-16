"""Tier 2: right-panel YAML dump renders multi-line strings as readable blocks.

Tool results reach the right panel verbatim, but plain ``yaml.safe_dump``
emits a multi-line string (email body, MCP ``text`` content block) as a
single-quoted *folded* scalar — every ``\\n`` becomes a blank line and
continuations are quote-indented, so the faithful content is illegible and
the operator falls back to the paraphrase-prone prose. ``_yaml_block_dump``
uses a block scalar (``|``) for multi-line strings instead.

These pin the two properties that matter, NOT the exact whitespace (= not a
format-pin):
  - **fidelity**: the rendered YAML round-trips back to the original value
    (rendering must never alter the verbatim result)
  - **readability**: a multi-line string uses block style (``|`` present);
    a single-line string does not
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import yaml

from reyn.tui.widgets.right_panel import _yaml_block_dump


def test_multiline_string_roundtrips_unchanged() -> None:
    """Tier 2: a multi-line string survives block-scalar dump → load unchanged."""
    body = "山田様\n\nお世話になっております。\n火曜14時で可能でしょうか。\n田中"
    out = _yaml_block_dump({"body": body})
    # Rendering must not corrupt the verbatim content.
    assert yaml.safe_load(out) == {"body": body}


def test_multiline_string_uses_block_style() -> None:
    """Tier 2: a newline-containing string renders as a block scalar (readable)."""
    out = _yaml_block_dump({"text": "Found 3 messages:\n1. a\n2. b\n3. c"})
    assert "|" in out, (
        "multi-line strings must use block style (|) so they read as text, "
        f"not a folded quoted scalar. Got:\n{out!r}"
    )
    # And the lines are intact (round-trip fidelity, not a format-pin).
    assert yaml.safe_load(out) == {"text": "Found 3 messages:\n1. a\n2. b\n3. c"}


def test_single_line_string_stays_plain() -> None:
    """Tier 2: a single-line string is NOT forced into block style."""
    out = _yaml_block_dump({"status": "ok"})
    assert "|" not in out, f"single-line value should stay plain, got:\n{out!r}"
    assert yaml.safe_load(out) == {"status": "ok"}


def test_nested_email_shape_roundtrips() -> None:
    """Tier 2: a realistic nested tool-result shape round-trips intact."""
    result = {
        "status": "ok",
        "data": {"messages": [{
            "from": "Tanaka <tanaka@example.co.jp>",
            "subject": "Re: 打ち合わせ",
            "body": "お世話になっております。\n明日の件、よろしくお願いします。",
        }]},
    }
    out = _yaml_block_dump(result)
    assert yaml.safe_load(out) == result
