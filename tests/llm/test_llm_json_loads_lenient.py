"""Tests for the shared lenient JSON parsing helper (reyn.llm.json_parse)."""
from __future__ import annotations

import json

import pytest

from reyn.llm.json_parse import loads_lenient


class TestStrictPassThrough:
    def test_clean_object_returns_same_as_json_loads(self):
        """Tier 2: clean JSON object parses identically to json.loads (Tier 1 path)."""
        text = '{"a": 1, "b": "hello", "c": [1, 2, 3]}'
        result = loads_lenient(text)
        assert result == json.loads(text)

    def test_nested_object(self):
        """Tier 2: nested JSON structure parses correctly via strict path."""
        text = '{"outer": {"inner": true}, "list": [null, false, 42]}'
        result = loads_lenient(text)
        assert result == json.loads(text)

    def test_strict_path_does_not_fire_callback(self):
        """Tier 2: on_raw_decode callback is NOT called for clean JSON (Tier 1 path)."""
        calls: list[tuple[int, str]] = []
        loads_lenient('{"x": 1}', on_raw_decode=lambda n, h: calls.append((n, h)))
        assert calls == []


class TestTrailingCommaRepair:
    def test_trailing_comma_object_recovers(self):
        """Tier 2: trailing comma in object is repaired by Tier 2 path."""
        result = loads_lenient('{"a": 1,}')
        assert result == {"a": 1}

    def test_trailing_comma_array_recovers(self):
        """Tier 2: trailing comma in array is repaired by Tier 2 path."""
        result = loads_lenient('[1, 2, 3,]')
        assert result == [1, 2, 3]

    def test_trailing_comma_does_not_fire_callback(self):
        """Tier 2: on_raw_decode callback is NOT called for trailing-comma repair (Tier 2 path)."""
        calls: list[tuple[int, str]] = []
        loads_lenient('{"a": 1,}', on_raw_decode=lambda n, h: calls.append((n, h)))
        assert calls == []


class TestTrailingGarbageRecover:
    def test_trailing_text_recovers_leading_object(self):
        """Tier 2: valid JSON object followed by trailing text is recovered via Tier 3 raw_decode."""
        text = '{"a": 1}\n\nsome explanation text'
        result = loads_lenient(text)
        assert result == {"a": 1}

    def test_trailing_text_fires_callback(self):
        """Tier 2: on_raw_decode callback fires with positive discarded_len for trailing garbage."""
        calls: list[tuple[int, str]] = []
        text = '{"a": 1}\n\nsome explanation text'
        result = loads_lenient(text, on_raw_decode=lambda n, h: calls.append((n, h)))
        assert result == {"a": 1}
        assert calls, "on_raw_decode must be called for trailing garbage"
        discarded_len, head = calls[-1]
        assert discarded_len > 0
        assert "some explanation text" in head

    def test_13977_style_large_trailing_garbage(self):
        """Tier 2: large valid JSON + trailing garbage (the 13977 failure pattern) recovers."""
        payload = {"control": {"type": "finish"}, "artifact": {"type": "x", "data": {}}}
        trailing = " extra content after the json object"
        text = json.dumps(payload) + trailing
        result = loads_lenient(text)
        assert result == payload

    def test_trailing_garbage_callback_head_capped_at_80(self):
        """Tier 2: on_raw_decode head argument is capped at 80 chars (not the full trailing string)."""
        calls: list[tuple[int, str]] = []
        long_garbage = "x" * 200
        text = '{"a": 1}' + long_garbage
        loads_lenient(text, on_raw_decode=lambda n, h: calls.append((n, h)))
        assert calls, "on_raw_decode must be called for trailing garbage"
        _, head = calls[-1]
        # head is capped at 80; the raw trailing is 200 chars, so head < trailing
        assert len(head) < len(long_garbage)


class TestTrailingWhitespaceNotFlagged:
    def test_trailing_whitespace_only_no_callback(self):
        """Tier 2: trailing whitespace after valid JSON does NOT fire the on_raw_decode callback."""
        calls: list[tuple[int, str]] = []
        text = '{"a": 1}\n  '
        result = loads_lenient(text, on_raw_decode=lambda n, h: calls.append((n, h)))
        assert result == {"a": 1}
        assert calls == [], "callback should not fire for whitespace-only trailing data"

    def test_trailing_newline_only_no_callback(self):
        """Tier 2: trailing newline after valid JSON does NOT fire the on_raw_decode callback."""
        calls: list[tuple[int, str]] = []
        text = '{"a": 1}\n'
        result = loads_lenient(text, on_raw_decode=lambda n, h: calls.append((n, h)))
        assert result == {"a": 1}
        assert calls == []


class TestGenuinelyMalformedRaises:
    def test_not_json_raises(self):
        """Tier 2: 'not json at all' raises json.JSONDecodeError (not recovered)."""
        with pytest.raises(json.JSONDecodeError):
            loads_lenient("not json at all")

    def test_leading_garbage_before_json_raises(self):
        """Tier 2: garbage before valid JSON ('garbage {...}') raises json.JSONDecodeError."""
        with pytest.raises(json.JSONDecodeError):
            loads_lenient('garbage {"a": 1}')

    def test_empty_string_raises(self):
        """Tier 2: empty string raises json.JSONDecodeError."""
        with pytest.raises(json.JSONDecodeError):
            loads_lenient("")

    def test_unclosed_object_raises(self):
        """Tier 2: genuinely unclosed JSON object raises json.JSONDecodeError."""
        with pytest.raises(json.JSONDecodeError):
            loads_lenient('{"a": 1')


class TestInvalidEscapeRepair:
    def test_lone_backslash_invalid_escape_recovers(self):
        """Tier 2: a bare backslash (invalid JSON escape) in a string value is repaired (D6/13453)."""
        invalid = '{"v": "a\\db"}'  # JSON source: single backslash before d = invalid escape
        with pytest.raises(json.JSONDecodeError):
            json.loads(invalid)
        assert loads_lenient(invalid) == {"v": "a\\db"}  # value = a, backslash, d, b

    def test_valid_escaped_backslash_unchanged(self):
        """Tier 2: an already-valid escaped backslash (\\\\d) is NOT corrupted — decisive falsification."""
        valid = '{"v": "a\\\\db"}'  # JSON source: a\\db (escaped backslash) = value a\db
        assert loads_lenient(valid) == json.loads(valid)
        assert loads_lenient(valid) == {"v": "a\\db"}

    def test_regex_in_reason_summary_recovers(self):
        """Tier 2: the 13453 pattern — a regex backslash in a free-text reason.summary — parses."""
        invalid = '{"control": {"reason": {"summary": "the \\d+ pattern matched"}}}'
        result = loads_lenient(invalid)
        assert result["control"]["reason"]["summary"] == "the \\d+ pattern matched"

    def test_multiple_invalid_escapes_in_one_string(self):
        """Tier 2: several lone backslashes in one value all get repaired."""
        invalid = '{"v": "\\d and \\w and \\s"}'
        assert loads_lenient(invalid) == {"v": "\\d and \\w and \\s"}

    def test_valid_escapes_preserved(self):
        """Tier 2: valid escapes (\\n \\t \\" \\uXXXX) survive the repair pass unchanged."""
        valid = '{"a": "line1\\nline2\\ttab", "b": "quote\\"here", "c": "\\u00e9"}'
        assert loads_lenient(valid) == json.loads(valid)

    def test_invalid_escape_does_not_fire_raw_decode_callback(self):
        """Tier 2: escape-repair (Tier 3) does NOT fire on_raw_decode — that is Tier 4 only."""
        calls: list = []
        loads_lenient('{"v": "a\\db"}', on_raw_decode=lambda n, h: calls.append(n))
        assert calls == []

    def test_combined_invalid_escape_and_trailing_comma(self):
        """Tier 2: a value with BOTH an invalid escape and a trailing comma recovers."""
        text = '{"v": "a\\db",}'
        assert loads_lenient(text) == {"v": "a\\db"}

    def test_backslash_only_repaired_inside_strings(self):
        """Tier 2: a valid object whose string holds backslashes parses; structure is untouched."""
        invalid = '{"k1": "\\path\\here", "k2": [1, 2], "k3": true}'
        assert loads_lenient(invalid) == {"k1": "\\path\\here", "k2": [1, 2], "k3": True}

    def test_13453_escaped_single_quote_decision_recovers(self):
        """Tier 2: the real 13453 failure — Python-style \\' escaped single quotes (invalid JSON
        escape) inside a free-text value break strict parse; the decision still recovers."""
        # The model wrote ...'AttributeError: \'HTML\' object...' — \' is an invalid JSON escape.
        # Double-quoted Python string: \\' -> the JSON source bytes \' (backslash + quote).
        raw = (
            "{\"type\": \"decide\", "
            "\"control\": {\"type\": \"transition\", \"next_phase\": \"plan\"}, "
            "\"artifact\": {\"data\": {\"failure_summary\": "
            "\"got \\'AttributeError\\' on \\'_set_col_formats\\'\"}}}"
        )
        assert "\\'" in raw  # the invalid escape is present in the JSON source
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)
        obj = loads_lenient(raw)
        assert obj["type"] == "decide"
        assert obj["control"]["next_phase"] == "plan"
        assert "AttributeError" in obj["artifact"]["data"]["failure_summary"]


class TestObservabilityOnlyOnTier3:
    def test_callback_not_called_for_tier1(self):
        """Tier 2: callback is never called when Tier 1 (strict) succeeds."""
        calls: list = []
        loads_lenient('{"ok": true}', on_raw_decode=lambda n, h: calls.append(n))
        assert calls == []

    def test_callback_not_called_for_tier2(self):
        """Tier 2: callback is never called when Tier 2 (repair) succeeds."""
        calls: list = []
        loads_lenient('[1,]', on_raw_decode=lambda n, h: calls.append(n))
        assert calls == []

    def test_callback_called_for_tier3(self):
        """Tier 2: callback IS called when Tier 3 (raw_decode) fires."""
        calls: list = []
        loads_lenient(
            '{"a": 1} trailing',
            on_raw_decode=lambda n, h: calls.append(n),
        )
        assert calls, "on_raw_decode must be called for trailing garbage"
        assert all(n > 0 for n in calls)
