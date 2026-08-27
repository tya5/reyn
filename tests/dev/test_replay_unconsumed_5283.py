"""Tier 1: Contract — reyn.dev.testing.replay_unconsumed, the #5283
structural gate (LLMReplay entries no replay hit consumed this session).

Same two-layer style ``test_network_gate_3451.py`` uses:

1. The event-file bookkeeping (``report_instance`` / ``unconsumed_by_fixture``
   / ``reset_events_file``) is pure file I/O — exercised directly.
2. A real ``LLMReplay`` instance drives one witness end to end (its OWN
   ``_replay`` hit path -> ``consumed_keys()`` -> ``report_instance`` ->
   ``unconsumed_by_fixture``), so the low-level bookkeeping being correct in
   isolation is not mistaken for the whole wire being correct.
3. ``pytester`` drives the fail-open / CI-red witnesses end to end, a real
   isolated inner pytest session loading only this plugin.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.dev.testing import replay_unconsumed

pytest_plugins = ["pytester"]

INNER_CONFTEST = 'pytest_plugins = ["reyn.dev.testing.replay_unconsumed"]\n'


# ── Layer 1: event-file bookkeeping (real file, no pytest session needed) ──


def test_report_instance_noop_without_check_flag(tmp_path, monkeypatch):
    """Tier 1: report_instance() writes nothing unless
    REYN_REPLAY_UNCONSUMED_CHECK=1 — fail-open extends to the write side,
    not just pytest_sessionfinish's own read-back."""
    events_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("REYN_REPLAY_UNCONSUMED_EVENTS_PATH", str(events_path))
    monkeypatch.delenv("REYN_REPLAY_UNCONSUMED_CHECK", raising=False)
    replay_unconsumed.report_instance("fixtures/x.jsonl", {"k1", "k2"}, {"k1"})
    assert not events_path.exists()


def test_unconsumed_key_is_named(tmp_path, monkeypatch):
    """Tier 1: a loaded key nothing consumed is reported for its fixture."""
    events_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("REYN_REPLAY_UNCONSUMED_EVENTS_PATH", str(events_path))
    monkeypatch.setenv("REYN_REPLAY_UNCONSUMED_CHECK", "1")
    replay_unconsumed.report_instance("fixtures/x.jsonl", {"k1", "k2"}, {"k1"})
    assert replay_unconsumed.unconsumed_by_fixture() == {"fixtures/x.jsonl": {"k2"}}


def test_fully_consumed_fixture_reports_nothing(tmp_path, monkeypatch):
    """Tier 1: every loaded key consumed -> no finding for that fixture."""
    events_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("REYN_REPLAY_UNCONSUMED_EVENTS_PATH", str(events_path))
    monkeypatch.setenv("REYN_REPLAY_UNCONSUMED_CHECK", "1")
    replay_unconsumed.report_instance("fixtures/x.jsonl", {"k1", "k2"}, {"k1", "k2"})
    assert replay_unconsumed.unconsumed_by_fixture() == {}


def test_never_opened_fixture_is_absent_not_reported(tmp_path, monkeypatch):
    """Tier 1: a fixture this session never opened carries no 'opened' event
    and is silently excluded — never reported as 100% unconsumed just
    because it exists on disk somewhere this run never touched."""
    events_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("REYN_REPLAY_UNCONSUMED_EVENTS_PATH", str(events_path))
    assert replay_unconsumed.unconsumed_by_fixture() == {}


def test_multiple_instances_against_same_file_union_correctly(tmp_path, monkeypatch):
    """Tier 1: two sibling tests opening the SAME fixture file each consume a
    different key — the union across both reports leaves nothing unconsumed,
    not each instance's own partial view (#3634's sibling-shared-fixture
    shape this gate must not misjudge)."""
    events_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("REYN_REPLAY_UNCONSUMED_EVENTS_PATH", str(events_path))
    monkeypatch.setenv("REYN_REPLAY_UNCONSUMED_CHECK", "1")
    replay_unconsumed.report_instance("fixtures/shared.jsonl", {"k1", "k2"}, {"k1"})
    replay_unconsumed.report_instance("fixtures/shared.jsonl", {"k1", "k2"}, {"k2"})
    assert replay_unconsumed.unconsumed_by_fixture() == {}


def test_reset_events_file_clears_prior_run(tmp_path, monkeypatch):
    """Tier 1: reset_events_file() is what pytest_configure calls (controller
    only) so a PRIOR run's leftover events never leak into this run."""
    events_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("REYN_REPLAY_UNCONSUMED_EVENTS_PATH", str(events_path))
    monkeypatch.setenv("REYN_REPLAY_UNCONSUMED_CHECK", "1")
    replay_unconsumed.report_instance("fixtures/x.jsonl", {"k1"}, set())
    replay_unconsumed.reset_events_file()
    assert replay_unconsumed.unconsumed_by_fixture() == {}


# ── Witness 2 (architect's ruling): a dead consumption-recorder must make
# the report go SILENT, never a false-positive flood ─────────────────────


def test_recorder_dead_canary_suppresses_the_report_entirely(tmp_path, monkeypatch):
    """Tier 1: if EVERY opened fixture's consumed set is empty while loaded
    keys exist (the shape ``_consumed_keys.add`` being stripped from
    ``LLMReplay._replay`` produces globally), the report must go EMPTY —
    not claim every single entry everywhere is unreachable. A report that
    cannot tell 'the detector died' from 'everything is unreachable' is a
    report whose positive findings cannot be trusted either."""
    events_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("REYN_REPLAY_UNCONSUMED_EVENTS_PATH", str(events_path))
    monkeypatch.setenv("REYN_REPLAY_UNCONSUMED_CHECK", "1")
    replay_unconsumed.report_instance("fixtures/a.jsonl", {"k1", "k2"}, set())
    replay_unconsumed.report_instance("fixtures/b.jsonl", {"k3"}, set())
    assert replay_unconsumed.unconsumed_by_fixture() == {}


def test_recorder_dead_canary_does_not_mask_a_real_partial_finding(tmp_path, monkeypatch):
    """Tier 1: the canary is a GLOBAL emptiness check, not per-fixture — a
    session with at least one genuine consumed key elsewhere must not have
    its real finding masked."""
    events_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("REYN_REPLAY_UNCONSUMED_EVENTS_PATH", str(events_path))
    monkeypatch.setenv("REYN_REPLAY_UNCONSUMED_CHECK", "1")
    replay_unconsumed.report_instance("fixtures/a.jsonl", {"k1", "k2"}, {"k1"})
    assert replay_unconsumed.unconsumed_by_fixture() == {"fixtures/a.jsonl": {"k2"}}


# ── Layer 2: a REAL LLMReplay instance drives its own consumption hit ──────


class _FakeResponse:
    def model_dump(self):
        return {
            "id": "fake",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok", "tool_calls": None},
                    "finish_reason": "stop",
                }
            ],
            "model": "openai/gemini-2.5-flash-lite",
            "object": "chat.completion",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


def test_real_llmreplay_instance_wires_through_to_the_report(tmp_path, monkeypatch):
    """Tier 1: a REAL LLMReplay(mode="replay") instance's own ``_replay`` hit
    path -> consumed_keys() -> report_instance() -> unconsumed_by_fixture()
    end to end — not the bookkeeping alone, the actual production object."""
    from reyn.dev.testing.replay import LLMReplay

    events_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("REYN_REPLAY_UNCONSUMED_EVENTS_PATH", str(events_path))
    monkeypatch.setenv("REYN_REPLAY_UNCONSUMED_CHECK", "1")

    fixture_path = tmp_path / "fixture.jsonl"
    model = "openai/gemini-2.5-flash-lite"
    hit_messages = [{"role": "user", "content": "hit me"}]
    miss_messages = [{"role": "user", "content": "never called"}]

    async def _fake_acompletion(**kwargs):
        return _FakeResponse()

    async def record_both():
        # monkeypatch.setattr, not unittest.mock.patch — the same technique
        # test_fp0063_arc_witness.py's own generate mode uses to script
        # litellm.acompletion in record mode without a real network call.
        monkeypatch.setattr("litellm.acompletion", _fake_acompletion, raising=False)
        replay = LLMReplay(fixture_path, mode="record")
        replay.install()
        try:
            import litellm

            await litellm.acompletion(model=model, messages=hit_messages)
            await litellm.acompletion(model=model, messages=miss_messages)
        finally:
            replay.restore()
            replay.flush()

    asyncio.run(record_both())
    recorded_key_count = len(fixture_path.read_text().splitlines())
    assert recorded_key_count > 1, "both messages must have recorded distinct entries"

    async def replay_only_one():
        replay = LLMReplay(fixture_path, mode="replay")
        replay.install()
        try:
            import litellm

            await litellm.acompletion(model=model, messages=hit_messages)
        finally:
            replay.restore()
        replay_unconsumed.report_instance(
            str(fixture_path), replay.loaded_keys(), replay.consumed_keys(),
        )

    asyncio.run(replay_only_one())

    hits = replay_unconsumed.unconsumed_by_fixture()
    assert str(fixture_path) in hits
    # The miss_messages entry — never replayed — must be the one named;
    # the hit_messages entry must NOT appear (it WAS consumed).
    from reyn.dev.testing.replay import LLMReplay as _LLMReplay

    miss_key = _LLMReplay.key(model, miss_messages)
    hit_key = _LLMReplay.key(model, hit_messages)
    assert miss_key in hits[str(fixture_path)]
    assert hit_key not in hits[str(fixture_path)]


# ── Witness 1 / 3 (architect's ruling): end-to-end via a real inner pytest
# session — CI-red when the flag is set, silent when it is not ───────────


def _run_inner(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, body: str, events_path: Path,
):
    pytester.makeconftest(INNER_CONFTEST)
    pytester.makepyfile(test_inner=body)
    monkeypatch.setenv("REYN_REPLAY_UNCONSUMED_EVENTS_PATH", str(events_path))
    return pytester.runpytest()


_SEED_UNCONSUMED_BODY = """
    def test_seed():
        from reyn.dev.testing import replay_unconsumed
        replay_unconsumed.report_instance(
            "fixtures/inner.jsonl", {"k1", "k2"}, {"k1"},
        )
"""


def test_witness1_unconsumed_entry_fails_the_inner_session_when_flag_set(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """Tier 1: architect's ruling, witness 1 — with
    REYN_REPLAY_UNCONSUMED_CHECK=1, an unconsumed entry makes the whole
    session exit non-zero and names the key — CI-red, per lead-coder's
    accept condition ("a report nobody's required to read is not a
    mechanism")."""
    monkeypatch.setenv("REYN_REPLAY_UNCONSUMED_CHECK", "1")
    result = _run_inner(pytester, monkeypatch, _SEED_UNCONSUMED_BODY, tmp_path / "events.jsonl")
    result.assert_outcomes(passed=1)
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*k2*"])


def test_witness3_fail_open_without_the_flag(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """Tier 1: architect's ruling, witness 3 — the SAME unconsumed data, but
    with no full-run signal set — the session must stay green. Never
    inferred from what flags were absent; only this explicit env var gates
    the check at all (architect's ruling, condition 1)."""
    monkeypatch.delenv("REYN_REPLAY_UNCONSUMED_CHECK", raising=False)
    result = _run_inner(pytester, monkeypatch, _SEED_UNCONSUMED_BODY, tmp_path / "events.jsonl")
    result.assert_outcomes(passed=1)
    assert result.ret == 0
    assert "UNCONSUMED" not in "\n".join(result.outlines)
