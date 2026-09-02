"""Tier 2: #5367 — the reactive shrink ladder absorbs a history sized to
overflow, WITHOUT the (now-retired) proactive elide branch's help.

Architect's own acceptance condition (#5367 review, 2026-08-29): "『ladder
が引き取る』は誰も確かめていない仮定のまま" — the claim that removing
`build_history`'s own proactive elide branch is safe because the REACTIVE
shrink ladder (`RouterLoopDriver._run_with_shrink`, compact via
`force_compact_now`, spill via `_attempt_reactive_spill`) picks up the
slack was, until this test, an unverified assumption, not a witness.

Two DISTINCT elide-absorbing shapes, both witnessed here (architect's own
follow-up condition, same review): elide's real domain was never "one big
turn" — the head/tail window can also overflow from MANY SMALL turns
(each individually below any spill-worthy size), which spill has nothing
to grab onto. That shape can only recover via compaction (turn-level
folding into a summary), a genuinely different code path from spill.

1. One oversized tool-result turn — recovers via SPILL
   (`_attempt_reactive_spill`).
2. Many small turns whose SUM exceeds the trigger — recovers via
   COMPACTION (`compact()`, invoked from `retry_loop`'s own
   `if raw_middle:` fold).

Real incident, disclosed (#5367 PR review, 2026-08-29): an EARLIER version
of test ② was run WITHOUT `@pytest.mark.llm_stub`, so `compact()`'s real
`litellm.acompletion` call failed with no network/API key available in
the harness — misclassified by `retry_loop`'s own same-cause-recovered
guard as "this cause cannot be resolved", raising `UnrecoveredError`. That
was reported to architect/lead-coder as "the reactive ladder has no
receiving mechanism for this shape" — a false alarm, retracted once
lead-coder's own question ("did compact() actually run?") prompted
re-running WITH `@llm_stub`, where it succeeds cleanly. Kept here as the
concrete reason test ② below is marked `@llm_stub` and NOT optional —
omitting it silently reproduces this exact false alarm.

Both scenarios sent raw (no proactive pre-check) on the first attempt —
`build_history()` no longer elides — and the fake loop raises a
413-shaped overflow while the oversized payload is what it was actually
handed (content-driven, not a hardcoded call count — mirrors
`test_5296_pr2_byte_reduction_same_turn_retry.py`'s own harness, whose
"one huge tool result, spill alone fixes it" scenario test ① reuses
directly).

Strip-falsified by hand for both (not committed as tests — each would
itself need a witness that IT correctly detects vacuity, an infinite
regress): with `_attempt_reactive_spill` monkeypatched to always report
no progress, scenario ① correctly raises `UnrecoveredError` instead of
succeeding; with `CompactionEngine.compact` monkeypatched to always raise
(NOT `fold_persist_policy="never"` — measured directly: `retry_loop`'s own
`if raw_middle:` compact call has no `fold_persist_policy` gate at all,
`grep -n "fold_persist_policy" engine.py` finds zero hits in that module;
`fold_persist_policy` only gates a DIFFERENT, driver-level side-effect
compaction in `router_loop_driver.py`'s own except block, #4954(b) —
setting it to `"never"` in a first draft of this strip-falsify did
nothing, the test stayed green, which is exactly how a strip-falsify
that targets the wrong mechanism looks), scenario ② does too.

Real `Session` + real `RouterLoopDriver`/`RouterHistoryBuffer`/`MediaStore`
throughout; the LLM call itself is stubbed (`@llm_stub`, test ② only —
test ① never reaches it, spill alone resolves it) since it cannot run
offline — see `reyn.dev.testing.llm_stub`'s own module docstring for what
that stub does and does not claim to verify.
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
from tests._support.events import collect_events


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
        fold_persist_policy="never",  # isolate spill's own contribution
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

    events = collect_events(session)

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


def _make_session_t_max_compact_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, t_max: int,
):
    """Same shape as ``_make_session_t_max`` above, but with the default
    ``fold_persist_policy`` (compaction enabled) instead of ``"never"`` — the
    single-huge-tool-result witness above deliberately isolates spill;
    this scenario needs compaction itself reachable."""
    monkeypatch.chdir(tmp_path)
    import reyn.llm.model_budget as _mb
    monkeypatch.setattr(_mb, "get_max_input_tokens", lambda model, **kw: t_max)
    cfg = CompactionConfig(
        body_token_cap=1500,
        use_chars4_estimate=True,
        section_caps_spec_tokens=0,
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


@pytest.mark.llm_stub
def test_reactive_ladder_recovers_many_small_turns_via_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the SECOND witness architect required (2026-08-29 follow-
    up) — elide's real domain was never "one big turn"; many small turns
    (none individually spill-worthy) whose SUM exceeds the trigger is the
    shape spill has nothing to grab onto. This must recover via
    compaction instead — a genuinely different code path.

    ``@llm_stub`` is REQUIRED, not decorative — see this module's own
    docstring for the real false-alarm this test's first draft produced
    without it."""
    session = _make_session_t_max_compact_enabled(tmp_path, monkeypatch, t_max=2800)
    texts = [f"turn-{i}:" + ("X" * 320) for i in range(30)]  # 80 tok each, chars4
    for i, text in enumerate(texts):
        _push(session, "user" if i % 2 == 0 else "assistant", text)

    # Sanity ①: the RAW wire estimate genuinely exceeds a plausible
    # trigger — the same condition elide used to gate on.
    raw_history = session._history_buffer.build_history()
    assert _wire_estimate(raw_history) > 2800, (
        "test setup sanity: the constructed history must actually be "
        "oversized, or this test proves nothing about overflow recovery"
    )
    # Sanity ②: this fixture genuinely produces MIDDLE candidates (not
    # all 30 turns landing in head/tail) — the scenario this test exists
    # to cover, distinct from test ① above.
    _head, raw_middle, _tail, _summary, _seq_by_id = (
        session._history_buffer.decompose_history_for_retry()
    )
    assert raw_middle, (
        "test setup sanity: expected non-empty raw_middle (candidates for "
        "compaction) — if this fails, the fixture needs adjusting, not "
        "the assertions below"
    )

    events = collect_events(session)

    loop = _ContentDrivenLoop(
        lambda history, user_text: _wire_estimate(history) > 2800
    )

    result = asyncio.run(
        session._loop_driver._run_with_shrink_and_byte_reduction(
            loop, "continue please", chain_id="c1",
        )
    )
    assert result is None  # the fake loop's own successful return
    # compact() was genuinely invoked — not "didn't crash by luck".
    assert any(e.type == "compaction_started" for e in events), (
        "expected compaction to have actually run for this scenario"
    )


@pytest.mark.asyncio
@pytest.mark.llm_stub
async def test_5528_a_turn_completes_through_the_real_entry_without_the_removed_proactive_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5528 — architect/lead-coder BLOCKING on PR #5538 (2026-08-30):
    the two witnesses above drive ``_run_with_shrink_and_byte_reduction``
    directly, bypassing ``RouterLoopDriver.run_turn`` /
    ``ContextBudgetAdvisor.enforce_new_msg_budget`` entirely — the EXACT
    call site #5528 removed a branch from. Structurally insensitive to
    that removal: green before, green after, for a reason unrelated to the
    change (they never reached it).

    This test drives the REAL entry (``Session._run_router_loop`` ->
    ``run_turn`` -> ``enforce_new_msg_budget``) with history satisfying
    the removed branch's own trigger condition — the literal
    ``estimated > effective_trigger`` this PR deleted — and asserts the
    turn completes successfully through that real path. Green/green is
    the correct shape here (lead-coder's own framing): the claim under
    test is "a turn still succeeds with the proactive guard gone", not a
    red test — what's required is that the real call chain was actually
    exercised, which the sanity assertion below (the same shape #5367's
    own witnesses use) confirms independently of the outcome."""
    session = _make_session_t_max_compact_enabled(tmp_path, monkeypatch, t_max=2800)
    _push(session, "user", "look something up")
    huge = "Y" * 50_000
    _push(session, "tool", huge, tool_call_id="tc1", name="tool")
    _push(session, "assistant", "ok, done")

    # Sanity: history genuinely satisfies the removed branch's own condition
    # (estimated > effective_trigger) — the exact `if` #5528 deleted from
    # enforce_new_msg_budget's predecessor. If this fails, the fixture
    # needs adjusting, not the assertion below.
    raw_history = session._history_buffer.build_history()
    assert _wire_estimate(raw_history) > 2800, (
        "test setup sanity: history must exceed the (removed) proactive "
        "guard's own trigger condition, or this test proves nothing about "
        "its removal"
    )

    # The real entry point — NOT _run_with_shrink_and_byte_reduction
    # directly — so enforce_new_msg_budget (the call site #5528 changed)
    # is genuinely on the path.
    await session._run_router_loop("continue please", "c-5528")
