"""Tests for scripts/rekey_fixtures.py.

Tier 2: OS-invariant — validates additive rekey behaviour without hitting LLM
or running the full pytest subprocess chain.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

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
    assert len(entries) == 2                          # original preserved
    assert entries[0]["key"] == "aaa111"              # order unchanged
    assert entries[1]["key"] == "bbb222"              # new entry appended
    assert entries[1]["response"] == old_entry["response"]  # response reused


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
    assert len(entries) == 1     # nothing added


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


def test_rekey_uses_last_entry_response_when_multiple(tmp_path):
    """Tier 2: when a fixture has multiple entries, the newest response is reused."""
    fixture = tmp_path / "fixture.jsonl"
    entries = [
        {
            "key": f"key{i}",
            "model": "gemini-2.5-flash-lite",
            "prompt_preview": "Q",
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
    assert len(result) == 4
    new_entry = result[-1]
    assert new_entry["key"] == "new_key_xyz"
    # Must reuse last (index 2) entry's response
    assert new_entry["response"]["choices"][0]["message"]["content"] == "resp2"
