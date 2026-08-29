"""Tier 2: #5367 — the reactive shrink ladder absorbs a history sized to
overflow, WITHOUT the (now-retired) proactive elide branch's help.

Architect's own acceptance condition (#5367 review, 2026-08-29): "『ladder
が引き取る』は誰も確かめていない仮定のまま" — the claim that removing
`build_history`'s own proactive elide branch is safe because the REACTIVE
shrink ladder (`RouterLoopDriver._run_with_shrink`, compact via
`force_compact_now`, spill via `_attempt_reactive_spill`) picks up the
slack was, until this test, an unverified assumption, not a witness.

Scenario: one oversized tool-result turn whose estimate exceeds
`effective_trigger` — the EXACT condition
`test_history_exceeds_trigger_elides_middle` (deleted, #5367) used to
construct to force the now-retired elide branch. Post-#5367,
`build_history()` sends this raw on the first attempt (no local
pre-check); the fake loop below raises a 413-shaped overflow while the
oversized payload is what it was actually handed (content-driven, not a
hardcoded call count — mirrors `test_5296_pr2_byte_reduction_same_turn_
retry.py`'s own harness, whose "one huge tool result, spill alone fixes
it" scenario this witness reuses directly), and the reactive ladder's
spill path must recover the SAME turn.

Strip-falsified by hand (not committed as a test — would itself need a
witness that IT correctly detects vacuity, an infinite regress): with
`_attempt_reactive_spill` monkeypatched to always report no progress,
this same scenario correctly raises `UnrecoveredError` instead of
succeeding — confirming the witness below is not vacuously green.

Real `Session` + real `RouterLoopDriver`/`RouterHistoryBuffer`/`MediaStore`
throughout — a real spill genuinely shrinks the wire payload; no fakes
except the LLM call itself (cannot run offline).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reyn.config import CompactionConfig, MultimodalConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.chat_message import ChatMessage
from tests._support.agent_session import make_session


class _FakeStatusError(Exception):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class _ContentDrivenLoop:
    """A fake ``RouterLoop`` whose ``run()`` raises a 413-shaped error
    exactly while ``should_fail(history)`` says so, driven by the REAL
    ``history`` payload it is handed on each call — mirrors
    ``test_5296_pr2_byte_reduction_same_turn_retry.py``'s own harness."""

    def __init__(self, should_fail) -> None:
        self._should_fail = should_fail
        self.calls: "list[list[dict]]" = []

    async def run(self, *, user_text: str, history: "list[dict]") -> "object | None":
        self.calls.append(history)
        if self._should_fail(history, user_text):
            raise _FakeStatusError("request too large", status_code=413)
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _push(session, role: str, text: str, **kw) -> None:
    session._append_history(ChatMessage(role=role, content=text, ts=_now(), **kw))


def _make_session_t_max(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, t_max: int):
    monkeypatch.chdir(tmp_path)
    import reyn.llm.model_budget as _mb
    monkeypatch.setattr(_mb, "get_max_input_tokens", lambda model, **kw: t_max)
    cfg = CompactionConfig(
        body_token_cap=1500,
        use_chars4_estimate=True,
        section_caps_spec_tokens=0,
        max_shrink_iterations=1,
        recovery_policy="never",  # isolate spill's own contribution
    )
    return make_session(
        agent_name="default",
        agent_role="",
        output_language="en",
        budget_tracker=BudgetTracker(CostConfig()),
        state_log=StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl"),
        compaction_config=cfg,
        multimodal_config=MultimodalConfig(),
        snapshot_path=tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json",
    )


def _wire_estimate(history: "list[dict]") -> int:
    return sum(len(str(m.get("content", ""))) for m in history)


def test_reactive_ladder_recovers_an_elide_sized_overflow_via_spill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the witness architect required — a history sized exactly
    like the old elide branch's own trigger condition (total estimate >>
    effective_trigger) is sent raw (no proactive elide), the first
    attempt overflows, and the REACTIVE ladder's spill recovers the SAME
    turn without a second, unrecoverable failure.

    One oversized tool result (mirrors ``test_5296_pr2_byte_reduction_
    same_turn_retry.py``'s own already-proven "spill alone fixes it"
    shape — a real ``max_shrink_iterations=1`` session, one recovery
    attempt) — the point of this witness is proving the reactive path
    exists and fires post-#5367, not stress-testing multi-candidate
    spill exhaustion (a separate concern, out of #5367's scope)."""
    session = _make_session_t_max(tmp_path, monkeypatch, t_max=2800)
    _push(session, "user", "look something up")
    huge = "Y" * 50_000
    _push(session, "tool", huge, tool_call_id="tc1", name="tool")
    _push(session, "assistant", "ok, done")

    # Sanity: the RAW wire estimate genuinely exceeds a plausible trigger —
    # the same "total > effective_trigger" condition elide used to gate on.
    raw_history = session._history_buffer.build_history()
    assert _wire_estimate(raw_history) > 2800, (
        "test setup sanity: the constructed history must actually be "
        "oversized, or this test proves nothing about overflow recovery"
    )

    events: list = []
    session._audit_events.add_subscriber(lambda e: events.append(e))

    loop = _ContentDrivenLoop(
        lambda history, user_text: _has_content(history, huge)
    )

    result = asyncio.run(
        session._loop_driver._run_with_shrink_and_byte_reduction(
            loop, "continue please", chain_id="c1",
        )
    )
    assert result is None  # the fake loop's own successful return
    # The FIRST call was raw — proves build_history() sent the full,
    # un-elided payload (no proactive pre-check standing in the way).
    assert _has_content(loop.calls[0], huge)
    # The LAST call succeeded — the reactive ladder genuinely recovered
    # this turn, not merely "didn't crash". (If these were the SAME call —
    # no retry ever happened — this and the assertion above would
    # contradict each other over the identical list, so this also proves
    # a real, distinct retry occurred, without pinning how many.)
    assert not _has_content(loop.calls[-1], huge), (
        "expected the retried call to no longer carry the raw (unspilled) "
        "tool body"
    )


def _has_content(history: "list[dict]", needle: str) -> bool:
    return any(needle in str(m.get("content", "")) for m in history)
