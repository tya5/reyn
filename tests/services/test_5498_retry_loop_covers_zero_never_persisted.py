"""Tier 2: #5498 — the ``covers_through_seq=0`` `CompactionEngine.compact()`
structurally produces for `retry_loop`'s own caller (its ``new_turns`` are
litellm wire dicts with no ``seq`` key — see
``SeqUnavailable.WIRE_DICTS_CARRY_NO_SEQ``'s own docstring, #5475) never
reaches ``history.jsonl``.

architect ruling (#5498): safety here rests on TWO independent facts —
(1) `retry_loop` never persists its own `ChatSummary` to history at all
(a pure TRANSPORT operation), (2) `CompactionController`'s own
``covers_through_seq or candidates[-1].seq`` masks a real 0 too, for a
DIFFERENT original reason (#4951-A). A test asserting only "retry_loop's
compaction produced no summary in history" is green even if
`history_appender` itself were dead (never called at all) — this file
puts BOTH paths in the SAME test: the controller path proves a summary
DOES land in history (the appender is genuinely alive), the retry_loop
path proves one does NOT, from a real, driven overflow-recovery cycle
(not a hand-built dict scenario with no real Session to persist into).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.dev.testing.llm_stub import LLMStub
from tests._support.events import settle
from tests.runtime.test_5296_pr2_byte_reduction_same_turn_retry import (
    _ContentDrivenLoop,
    _make_spill_session,
    _push,
)


@pytest.mark.asyncio
async def test_controller_summary_lands_in_history_but_retry_loop_summary_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: two real, driven paths in one test — the accept-side witness
    (controller path: a summary DOES land) proves the appender this test
    depends on for its OWN negative claim is genuinely alive, not merely
    unreached."""
    # ── Path 1: CompactionController — a real summary lands in history ──
    ctrl_session = _make_spill_session(tmp_path, monkeypatch, t_max=7_000)
    for i in range(30):
        _push(ctrl_session, "user", f"filler turn {i} " * 40)

    stub = LLMStub()
    stub.install()
    try:
        await ctrl_session._compaction_controller.force_compact_now()
        await settle(ctrl_session)
    finally:
        stub.restore()

    ctrl_summaries = [m for m in ctrl_session.history if m.role == "summary"]
    assert ctrl_summaries, (
        "sanity: the controller path must produce a real summary entry — "
        "if this fails, history_appender itself is dead, and the retry_loop "
        "side below would pass VACUOUSLY (both prove nothing)"
    )
    assert ctrl_summaries[0].meta.get("structured", {}).get("covers_through_seq"), (
        "the controller path's own summary must carry a real, nonzero "
        "covers_through_seq — the `or candidates[-1].seq` fallback "
        "(compaction_controller.py) is what guarantees this"
    )

    # ── Path 2: retry_loop's own internal compaction — no summary lands ──
    retry_tmp = tmp_path / "retry"
    retry_tmp.mkdir()
    retry_session = _make_spill_session(
        retry_tmp, monkeypatch, t_max=2_500,
        max_shrink_iterations=25, recovery_policy="never",
    )
    budgets = retry_session._compaction_controller._engine.budgets
    marker_text = "OVERSIZED_MARKER_5498"
    marker_tokens = max(1, len(marker_text) // 4)
    _FILLER_COUNT = 4
    assert _FILLER_COUNT <= marker_tokens
    head_tokens = budgets.effective_trigger + budgets.tail_budget + 1_000
    head_text = "H" * (head_tokens * 4)
    per_filler_tokens = max(1, budgets.tail_budget // _FILLER_COUNT)
    filler_text = "F" * (per_filler_tokens * 4)

    calls_seen: "list[bool]" = []

    def _raise_on_marker_content(messages: list) -> bool:
        has_marker = marker_text in messages[-1].get("content", "")
        calls_seen.append(has_marker)
        return has_marker

    stub2 = LLMStub(raise_for=_raise_on_marker_content, cause="byte_limit")
    stub2.install()
    try:
        _push(retry_session, "user", head_text)
        _push(retry_session, "tool", marker_text, tool_call_id="tc-marker", name="big_tool")
        for _i in range(_FILLER_COUNT):
            _push(retry_session, "user", filler_text)

        _seen_first = {"done": False}

        def _fail_only_first(history: list, user_text: str) -> bool:
            if _seen_first["done"]:
                return False
            _seen_first["done"] = True
            return True

        loop = _ContentDrivenLoop(_fail_only_first)
        await retry_session._loop_driver._run_with_shrink(
            loop, "continue please", chain_id="c1",
        )
        await settle(retry_session)
    finally:
        stub2.restore()

    # Positive engagement witness — retry_loop's own compact() genuinely
    # ran on real content (not zero calls, which would make the negative
    # assertion below vacuous the same way an unreached appender would).
    assert True in calls_seen, (
        "sanity: retry_loop's own compact() must have actually run against "
        "the marker content — otherwise the absence check below proves "
        "nothing"
    )

    retry_summaries = [m for m in retry_session.history if m.role == "summary"]
    assert not retry_summaries, (
        f"retry_loop's own internal compaction must NOT persist a summary "
        f"to history — it is a pure TRANSPORT operation (see "
        f"CompactionEngine.compact()'s own #5498 comment); found "
        f"{len(retry_summaries)} unexpected summary entries"
    )
