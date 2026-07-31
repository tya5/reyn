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


# ── #3568: the patch must not drift away from what it patches ─────────────────


def test_patch_signature_matches_real_llm_replay():
    """Tier 2: #3568 — the script's declared patch signature matches the REAL
    LLMReplay._replay. When _replay's parameters are renamed, reordered, or
    added to, the patch can no longer bind and the tool observes NOTHING; the
    pre-#3473 4-parameter patch made every patched call raise TypeError while
    the tool printed 'all fixtures up to date'. This assertion is the drift
    gate: it fails on the signature change, not months later on a false
    all-clear."""
    from reyn.dev.testing.replay import LLMReplay

    problem = rk.verify_replay_signature(LLMReplay)

    assert problem is None, problem


def test_incompatibility_report_names_the_parameters_that_moved():
    """Tier 2: #3568 — the refusal message names WHICH parameters differ, so the
    maintainer can fix the patch instead of guessing. Reproduces the historical
    drift: the pre-#3473 patch list vs the real signature of today."""
    import inspect

    from reyn.dev.testing.replay import LLMReplay

    stale = ["self", "key", "model", "messages"]          # the pre-#3473 patch
    actual = list(inspect.signature(LLMReplay._replay).parameters)

    report = rk._describe_incompatibility(stale, actual)

    assert "observed" in report and "request" in report
    assert "Nothing was scanned" in report


def test_zero_replay_calls_is_reported_as_a_problem_not_as_up_to_date():
    """Tier 2: #3568 — '0 missing keys' and 'the patch never fired' are different
    states and only the first is success. A scan that observed nothing must be
    diagnosed as unusable even though its missing-key list is empty."""
    observed_nothing = rk.ScanResult(missing=[], replay_calls=0, returncode=0)
    observed_something = rk.ScanResult(missing=[], replay_calls=3, returncode=0)

    problem = rk.diagnose_scan(observed_nothing)

    assert problem is not None
    assert "never fired" in problem
    assert "NOT 'all fixtures up to date'" in problem
    # the same empty missing-list, but with observations behind it, IS success
    assert rk.diagnose_scan(observed_something) is None


def test_missing_patcher_report_is_reported_as_a_problem():
    """Tier 2: #3568 — ``replay_calls is None`` (the patcher never reported at
    all, e.g. it died before exit) is a third state, distinct from 0 calls and
    from a clean scan, and is likewise not success."""
    never_reported = rk.ScanResult(missing=[], replay_calls=None, returncode=1)

    problem = rk.diagnose_scan(never_reported)

    assert problem is not None
    assert "measured nothing" in problem


def test_patch_error_line_becomes_the_diagnosis():
    """Tier 2: #3568 — when the patcher subprocess refuses to run, the parent
    reports the incompatibility rather than the empty missing-key list."""
    line = rk.MARKER_PATCH_ERROR + json.dumps({"actual": ["self", "k", "m"]})
    calls_line = rk.MARKER_CALLS + "0"
    output = f"{line}\n{calls_line}\n"

    result = rk.ScanResult(
        missing=rk._parse_missing_keys(output),
        replay_calls=rk.parse_replay_calls(output),
        patch_error=rk.parse_patch_error(output),
        returncode=rk.EXIT_PATCH_INCOMPATIBLE,
        output=output,
    )
    problem = rk.diagnose_scan(result)

    assert problem is not None
    assert "signature is incompatible" in problem


def test_call_count_is_read_from_the_patchers_self_report():
    """Tier 2: #3568 — the call count is read from the patcher's own line;
    output without that line yields None (never reported), not 0 (reported
    zero)."""
    assert rk.parse_replay_calls(f"noise\n{rk.MARKER_CALLS}7\nmore noise\n") == 7
    assert rk.parse_replay_calls("no patcher line here\n") is None


def test_rekeyed_entry_carries_preconditions_and_key_components(tmp_path):
    """Tier 2: #3568/#3473 — the appended entry has the SHAPE a record-mode entry
    has. Without ``preconditions`` the entry is 'precondition unverifiable' on
    any machine whose environment is non-empty; without ``key_components`` a
    later miss cannot be attributed to a component."""
    fixture = tmp_path / "fixture.jsonl"
    _write_fixture(fixture, [{
        "key": "old_key",
        "kind": "completion",
        "model": "gemini-2.5-flash-lite",
        "prompt_preview": "hi",
        "response": {"choices": []},
    }])

    rk._rekey_fixture(
        fixture_path=fixture,
        new_key="new_key",
        prompt_preview="hi",
        dry_run=False,
        preconditions={"mcp_catalog": {"servers": [], "tools": []}},
        key_components={"model": "abc", "tool_choice": "none"},
    )

    written = _read_fixture(fixture)[-1]
    assert written["key"] == "new_key"
    assert written["kind"] == "completion"
    assert written["preconditions"] == {"mcp_catalog": {"servers": [], "tools": []}}
    assert written["key_components"] == {"model": "abc", "tool_choice": "none"}


def test_rekeyed_entry_omits_stale_key_components_when_none_captured(tmp_path):
    """Tier 2: #3568 — a fingerprint is NOT copied from the source entry. It
    describes the OLD request; carried forward it would attribute the next miss
    to the wrong component, which is worse than having no attribution. The
    environment imprint, which is not request-specific, IS carried forward."""
    fixture = tmp_path / "fixture.jsonl"
    _write_fixture(fixture, [{
        "key": "old_key",
        "kind": "completion",
        "model": "gemini-2.5-flash-lite",
        "prompt_preview": "hi",
        "preconditions": {"mcp_catalog": {"servers": [], "tools": []}},
        "key_components": {"model": "stale-fingerprint"},
        "response": {"choices": []},
    }])

    rk._rekey_fixture(
        fixture_path=fixture, new_key="new_key", prompt_preview="hi", dry_run=False,
    )

    written = _read_fixture(fixture)[-1]
    assert "key_components" not in written
    assert written["preconditions"] == {"mcp_catalog": {"servers": [], "tools": []}}
