"""Tests for scripts/rekey_fixtures.py.

Tier 2: OS-invariant — validates additive rekey behaviour without hitting LLM
or running the full pytest subprocess chain.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make scripts/ importable
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
import rekey_fixtures as rk

# ── Helpers ────────────────────────────────────────────────────────────────────


def _write_fixture(path: Path, entries: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_fixture(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_rekey_appends_new_key_preserves_existing(tmp_path):
    """Tier 2: rekey appends a new entry and does NOT remove existing ones."""
    fixture = tmp_path / "fixture.jsonl"
    old_entry = {
        "key": "aaa111",
        "model": "gemini-2.5-flash-lite",
        "prompt_preview": "Hello?",
        "response": {"choices": [{"message": {"content": "Hi!"}}]},
    }
    _write_fixture(fixture, [old_entry])

    changed = rk._rekey_fixture(
        fixture_path=fixture,
        new_key="bbb222",
        prompt_preview="Hello?",
        dry_run=False,
    )

    assert changed is True
    entries = _read_fixture(fixture)
    assert entries[0]["key"] == "aaa111"              # original preserved, order unchanged
    assert entries[-1]["key"] == "bbb222"             # new entry appended
    assert entries[-1]["response"] == old_entry["response"]  # response reused


def test_rekey_skips_if_key_already_present(tmp_path):
    """Tier 2: rekey is idempotent — skip if the new key already exists."""
    fixture = tmp_path / "fixture.jsonl"
    entry = {
        "key": "existing_key",
        "model": "gemini-2.5-flash-lite",
        "prompt_preview": "Hi",
        "response": {"choices": []},
    }
    _write_fixture(fixture, [entry])

    changed = rk._rekey_fixture(
        fixture_path=fixture,
        new_key="existing_key",    # already there
        prompt_preview="Hi",
        dry_run=False,
    )

    assert changed is False
    entries = _read_fixture(fixture)
    assert entries, "fixture must still contain entries after no-op rekey"
    assert all(e["key"] == "existing_key" for e in entries), "no new entry should be added"


def test_dry_run_does_not_write(tmp_path):
    """Tier 2: dry_run=True reports intent but leaves the file unchanged."""
    fixture = tmp_path / "fixture.jsonl"
    entry = {
        "key": "old_key",
        "model": "gemini-2.5-flash-lite",
        "prompt_preview": "test",
        "response": {"choices": []},
    }
    _write_fixture(fixture, [entry])
    original_text = fixture.read_text()

    changed = rk._rekey_fixture(
        fixture_path=fixture,
        new_key="brand_new_key",
        prompt_preview="test",
        dry_run=True,
    )

    assert changed is True
    assert fixture.read_text() == original_text   # file must not change


def test_rekey_tiebreak_most_recent_when_previews_identical(tmp_path):
    """Tier 2: when several entries share the SAME preview (an ambiguous match),
    the most-recent (last) match is reused — the tie-break (#2024)."""
    fixture = tmp_path / "fixture.jsonl"
    entries = [
        {
            "key": f"key{i}",
            "model": "gemini-2.5-flash-lite",
            "prompt_preview": "Q",   # all identical → ambiguous match
            "response": {"choices": [{"message": {"content": f"resp{i}"}}]},
        }
        for i in range(3)
    ]
    _write_fixture(fixture, entries)

    rk._rekey_fixture(
        fixture_path=fixture,
        new_key="new_key_xyz",
        prompt_preview="Q",
        dry_run=False,
    )

    result = _read_fixture(fixture)
    assert result, "fixture must contain entries after rekey"
    new_entry = result[-1]
    assert new_entry["key"] == "new_key_xyz"
    # tie-break: most-recent match (index 2)
    assert new_entry["response"]["choices"][0]["message"]["content"] == "resp2"


def test_rekey_multiround_matches_per_entry_not_last(tmp_path):
    """Tier 2: #2024 bug 2 — a multi-round fixture (DISTINCT previews per round)
    re-keys each entry to ITS OWN matched response, NOT the last entry's. The old
    last-entry reuse gave every round the final round's response (corruption)."""
    fixture = tmp_path / "fixture.jsonl"
    entries = [
        {
            "key": "round1_oldkey",
            "model": "gemini-2.5-flash-lite",
            "prompt_preview": "round one: what is X?",
            "response": {"choices": [{"message": {"content": "X is one"}}]},
        },
        {
            "key": "round2_oldkey",
            "model": "gemini-2.5-flash-lite",
            "prompt_preview": "round two: what is Y?",
            "response": {"choices": [{"message": {"content": "Y is two"}}]},
        },
    ]
    _write_fixture(fixture, entries)

    # re-key round 1 (preview matches the FIRST entry, not the last)
    rk._rekey_fixture(
        fixture_path=fixture, new_key="round1_newkey",
        prompt_preview="round one: what is X?", dry_run=False,
    )
    # re-key round 2
    rk._rekey_fixture(
        fixture_path=fixture, new_key="round2_newkey",
        prompt_preview="round two: what is Y?", dry_run=False,
    )

    by_key = {e["key"]: e for e in _read_fixture(fixture)}
    # round 1's new key reuses round 1's response (the bug: it would be "Y is two")
    assert by_key["round1_newkey"]["response"]["choices"][0]["message"]["content"] == "X is one"
    assert by_key["round2_newkey"]["response"]["choices"][0]["message"]["content"] == "Y is two"


def test_rekey_skips_when_no_preview_match(tmp_path):
    """Tier 2: #2024 — when no existing entry's preview matches, the rekey is
    skipped (no write) rather than reusing an unrelated response (no silent
    corruption)."""
    fixture = tmp_path / "fixture.jsonl"
    entry = {
        "key": "only_key",
        "model": "gemini-2.5-flash-lite",
        "prompt_preview": "the recorded request",
        "response": {"choices": [{"message": {"content": "recorded"}}]},
    }
    _write_fixture(fixture, [entry])
    before = fixture.read_text()

    changed = rk._rekey_fixture(
        fixture_path=fixture, new_key="unmatched_newkey",
        prompt_preview="a completely different request", dry_run=False,
    )

    assert changed is False
    assert fixture.read_text() == before  # nothing written


def test_parse_missing_keys_json_newline_safe(tmp_path):
    """Tier 2: #2024 bug 1 — _parse_missing_keys reads the JSON MISSING_KEY line
    and preserves a preview containing a newline + a '|' (the old |-split form
    truncated at the first newline). Interleaved pytest output is ignored."""
    preview = "line one of the prompt\nline two with a | pipe"
    line = "MISSING_KEY=" + json.dumps({
        "new_key": "abc123def456",
        "fixture_path": "tests/fixtures/replay/foo.jsonl",
        "prompt_preview": preview,
    })
    output = "\n".join([
        "============ test session starts ============",
        "tests/test_x.py::test_y FAILED",
        line,
        "1 failed in 0.5s",
    ])

    parsed = rk._parse_missing_keys(output)

    # exactly this key parsed (content, not count) — interleaved pytest lines ignored
    assert [r["new_key"] for r in parsed] == ["abc123def456"]
    rec = parsed[0]
    assert rec["fixture_path"] == Path("tests/fixtures/replay/foo.jsonl")
    assert rec["prompt_preview"] == preview  # full preview preserved (newline + pipe)


def test_parse_missing_keys_dedups(tmp_path):
    """Tier 2: #2024 — duplicate (new_key, fixture_path) lines collapse to one."""
    rec = json.dumps({"new_key": "k1", "fixture_path": "f.jsonl", "prompt_preview": "p"})
    output = f"MISSING_KEY={rec}\nMISSING_KEY={rec}\n"
    parsed = rk._parse_missing_keys(output)
    assert [r["new_key"] for r in parsed] == ["k1"]  # deduped to the one request
