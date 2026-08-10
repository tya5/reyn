"""Tier 2: llm/json_parse._repair_trailing_commas + _escape_invalid_backslashes.

These two string-repair helpers form the spine of the lenient JSON parse
ladder (Tiers 2 and 3).  Their direct unit contracts are not covered by the
existing loads_lenient integration tests — only the composed behaviour is
exercised there.

_repair_trailing_commas: removes a single trailing comma before } or ] via
a regex substitution.  Does not nest; the regex fires once per closing char.

_escape_invalid_backslashes: walks the text in in-string/out-of-string state
and doubles any lone backslash inside a JSON string that is NOT followed by a
valid JSON escape char (\\, \", /, \b, \f, \n, \r, \t, \\uXXXX).  Pairwise
consumption keeps already-valid pairs intact.
"""
from __future__ import annotations

from reyn.llm.json_parse import _escape_invalid_backslashes, _repair_trailing_commas

# ── _repair_trailing_commas ───────────────────────────────────────────────────


def test_repair_trailing_commas_object_trailing_comma() -> None:
    """Tier 2: trailing comma before } is removed."""
    result = _repair_trailing_commas('{"a": 1,}')
    assert result == '{"a": 1}'


def test_repair_trailing_commas_array_trailing_comma() -> None:
    """Tier 2: trailing comma before ] is removed."""
    result = _repair_trailing_commas('[1, 2, 3,]')
    assert result == '[1, 2, 3]'


def test_repair_trailing_commas_no_trailing_comma_unchanged() -> None:
    """Tier 2: valid JSON without a trailing comma is returned unchanged."""
    text = '{"a": 1, "b": [1, 2]}'
    assert _repair_trailing_commas(text) == text


def test_repair_trailing_commas_empty_string_unchanged() -> None:
    """Tier 2: empty string passes through."""
    assert _repair_trailing_commas("") == ""


def test_repair_trailing_commas_nested_outer_comma_removed() -> None:
    """Tier 2: trailing comma on outer object removed; inner value unchanged."""
    text = '{"a": {"b": 1},}'
    result = _repair_trailing_commas(text)
    assert result == '{"a": {"b": 1}}'


def test_repair_trailing_commas_whitespace_preserved() -> None:
    """Tier 2: whitespace between trailing comma and closing bracket preserved."""
    text = '{"a": 1,\n}'
    result = _repair_trailing_commas(text)
    assert result.endswith("\n}")
    assert "a" in result


# ── _escape_invalid_backslashes ───────────────────────────────────────────────


def test_escape_invalid_backslashes_lone_d_escaped() -> None:
    r"""Tier 2: \d inside a JSON string (invalid escape) is doubled."""
    text = r'{"v": "a\db"}'
    result = _escape_invalid_backslashes(text)
    # The lone \d should become \\d so the string is valid JSON
    import json
    parsed = json.loads(result)
    assert parsed["v"] == r"a\db"


def test_escape_invalid_backslashes_valid_newline_unchanged() -> None:
    r"""Tier 2: \n (valid JSON escape) is left intact."""
    text = '{"v": "line1\\nline2"}'
    result = _escape_invalid_backslashes(text)
    assert result == text


def test_escape_invalid_backslashes_escaped_backslash_unchanged() -> None:
    r"""Tier 2: \\ (escaped backslash, valid) is preserved as-is."""
    text = r'{"v": "a\\b"}'
    result = _escape_invalid_backslashes(text)
    assert result == text


def test_escape_invalid_backslashes_already_doubled_d_unchanged() -> None:
    r"""Tier 2: \\d (escaped backslash followed by d) survives pairwise consumption.

    The \\ is consumed as a valid pair; d becomes a normal char.
    The pairwise rule avoids double-escaping already-correct input.
    """
    text = r'{"v": "a\\db"}'
    result = _escape_invalid_backslashes(text)
    assert result == text


def test_escape_invalid_backslashes_outside_string_unchanged() -> None:
    """Tier 2: content outside JSON strings is passed through unchanged."""
    text = 'no strings here'
    result = _escape_invalid_backslashes(text)
    assert result == text


def test_escape_invalid_backslashes_empty_string_unchanged() -> None:
    """Tier 2: empty input → empty output."""
    assert _escape_invalid_backslashes("") == ""
