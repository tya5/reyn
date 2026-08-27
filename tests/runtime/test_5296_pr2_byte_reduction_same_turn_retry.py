"""Tier 2: #5296 PR-2 — same-turn recovery from a BYTE-limited (HTTP 413)
unrecovered overflow.

Before this, `RouterLoopDriver._run_with_shrink`'s own `UnrecoveredError`
(with `.saw_byte_limit=True`) always ended the turn — #4954(b)'s own
`recovery_policy="next_turn"` only advanced the watermark for a LATER turn
via a real `force_compact_now()`, but THIS turn still failed. #5296 PR-2's
`_run_with_shrink_and_byte_reduction` (the new wrapper `run_turn` now calls
instead of the bare `_run_with_shrink`) intervenes on exactly that one
failure shape: spill first (reusing the existing `MediaStore.save_tool_
result` + `tool_result_cap.cap_tool_result_content` machinery via a new
session-lived, non-durable `RouterHistoryBuffer` projection overlay — never
`self.history`/`history.jsonl`, never the compaction watermark), then
durable compaction only if spill made no progress, then re-tries
`_run_with_shrink` — bounded by `_MAX_BYTE_REDUCTION_ATTEMPTS`.

Real `Session` + real `RouterLoopDriver`/`RouterHistoryBuffer`/`MediaStore`
throughout — the same harness `test_retry_loop_chat_wiring_1125.py`'s own
`_run_with_shrink` tests use (`session._loop_driver._run_with_shrink(...)`
driven directly, a scripted fake `loop.run`, since a real RouterLoop LLM
call cannot run offline). The fake loop here is CONTENT-driven (raises 413
based on what `history` it was actually handed, not a hardcoded call
count) — genuinely exercises whether spill/compaction changed the payload,
not an assumption about how many attempts it takes.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.config import CompactionConfig, MultimodalConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.chat_message import ChatMessage
from reyn.services.compaction.engine import UnrecoveredError
from tests._support.agent_session import make_session


class _FakeStatusError(Exception):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class _NoNetworkCompactionEngine:
    """A real-shaped, network-free ``CompactionEngine`` stand-in — mirrors
    ``test_pr_n6_compaction_overflow_retry.py``'s own ``_OverflowingEngine``
    (the sibling PR-N6 test file's documented way to exercise ``retry_loop``
    without a real LLM call). ``compact()`` always succeeds trivially,
    matching whatever ``covers_through_seq`` the offered turns imply — a
    real ``CompactionEngine.compact()`` would attempt an actual litellm
    call, which this test's `_run_with_shrink_and_byte_reduction` harness
    (driven through a real Session, unlike the sibling file's own
    ``retry_loop``-direct calls) has no way to stub via a `main_call`
    parameter alone; `force_compact_now`/retry_loop's own raw_middle fold
    BOTH reach this same seam."""

    def __init__(self) -> None:
        from reyn.core.events.events import EventLog
        from reyn.services.compaction.engine import ComputedBudgets
        self.budgets = ComputedBudgets(
            main_pool=10_000, head_budget=1_000, body_budget=500,
            tail_budget=1_500, new_msg_budget=1_000,
            B_M=8_000, main_M_room=7_000, effective_trigger=7_000,
            section_caps={
                "topic_arc": 50, "decisions": 200, "pending": 150,
                "session_user_facts": 50, "artifacts_referenced": 175,
            },
        )
        self._events = EventLog()
        self._T_comp_SP = 100
        self._model = "openai/test-standard-model"

    async def compact(self, input_chunk):
        from reyn.services.compaction.engine import ChatSummary

        def _seq(t: object) -> int:
            if isinstance(t, dict):
                return t.get("seq", 0)
            return getattr(t, "seq", 0)

        return ChatSummary(
            topic_arc="stub summary",
            covers_through_seq=max((_seq(t) for t in input_chunk.new_turns), default=0),
        )


def _inject_fake_engine(session) -> None:
    """Bypass ``CompactionController``'s lazy real-engine factory (private,
    name-mangled cache field — the same seam that property's own docstring
    names as "computed at most once") with a network-free stand-in, BEFORE
    anything triggers the real factory."""
    session._compaction_controller._CompactionController__engine_cache = (
        _NoNetworkCompactionEngine()
    )


class _ContentDrivenLoop:
    """A fake ``RouterLoop`` whose ``run()`` raises a 413-shaped error
    exactly while ``should_fail(history)`` says so, driven by the REAL
    ``history`` payload it is handed on each call — never a hardcoded
    call-count script, so a test genuinely exercises whether a reduction
    attempt changed the wire payload rather than assuming a fixed shape."""

    def __init__(self, should_fail) -> None:
        self._should_fail = should_fail
        self.calls: "list[list[dict]]" = []

    async def run(self, *, user_text: str, history: "list[dict]") -> "object | None":
        self.calls.append(history)
        if self._should_fail(history, user_text):
            raise _FakeStatusError("request too large", status_code=413)
        return None


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _push(session, role: str, text: str, **kw) -> None:
    # `_append_history` (not a bare `session.history.append`) — the real
    # write path, WAL-durable. `force_compact_now`'s own candidate read is
    # from the DURABLE store (`history.jsonl`), never `session.history`
    # directly (#4472's own "residency has no influence" invariant) — a
    # plain in-memory append would make every compaction test below
    # observe zero durable turns regardless of how much was pushed
    # (measured directly while building this test: `forced_sync_no_turns`
    # for 2000 in-memory-only turns).
    session._append_history(ChatMessage(role=role, content=text, ts=_now(), **kw))


def _make_spill_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
    max_shrink_iterations: int = 1, t_max: "int | None" = None,
    fake_compaction_engine: bool = False,
):
    """A real Session with a real MediaStore (default ``make_session`` gives
    ``media_store=None`` — the spill mechanism needs a real one to have any
    effect at all). ``t_max`` (mirrors ``test_retry_loop_chat_wiring_1125.
    py``'s own harness) forces a small ``effective_trigger`` so a real
    history genuinely produces compaction candidates instead of fitting
    comfortably under the real (fallback ~128k-token) model window.
    ``fake_compaction_engine`` swaps in ``_NoNetworkCompactionEngine`` — ONLY
    the one scenario that needs a `compact()` call to actually SUCCEED
    (rather than merely be attempted and fail/be avoided) needs this; the
    others never let ``compact()`` run for real at all."""
    monkeypatch.chdir(tmp_path)
    if t_max is not None:
        import reyn.llm.model_budget as _mb
        monkeypatch.setattr(_mb, "get_max_input_tokens", lambda model, **kw: t_max)
    cfg = CompactionConfig(
        body_token_cap=1500,
        use_chars4_estimate=True,
        section_caps_spec_tokens=0,
        max_shrink_iterations=max_shrink_iterations,
    )
    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    bt = BudgetTracker(CostConfig())
    session = make_session(
        agent_name="default",
        agent_role="",
        output_language="en",
        budget_tracker=bt,
        state_log=state_log,
        compaction_config=cfg,
        multimodal_config=MultimodalConfig(),
        snapshot_path=tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json",
    )
    if fake_compaction_engine:
        _inject_fake_engine(session)
    return session


def _has_content(history: "list[dict]", needle: str) -> bool:
    return any(needle in str(m.get("content", "")) for m in history)


# ── ② one huge tool result — spill fixes it, watermark does not move ────────


def test_single_huge_tool_result_recovers_via_spill_not_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: contract acceptance ② — a single oversized tool result is
    the ONLY large thing in history. Spill alone must fix it (watermark
    unchanged — no `compaction_check`/`compaction_completed` event)."""
    session = _make_spill_session(tmp_path, monkeypatch)
    _push(session, "user", "look something up")
    huge = "Y" * 50_000
    _push(session, "tool", huge, tool_call_id="tc1", name="tool")
    _push(session, "assistant", "ok, done")

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

    # `_run_with_shrink`'s own PRE-EXISTING #4954(b) next_turn side-effect
    # (untouched by this PR, architect ruling) opportunistically compacts
    # on EVERY byte-limited UnrecoveredError regardless of what this
    # wrapper does next — so a `compaction_check` event existing at all is
    # expected. What matters for THIS test is that it found nothing to
    # ACTUALLY compact — either no durable turns at all
    # (`outcome="forced_sync_no_turns"`) or a real pass that selected zero
    # middle candidates (`outcome="forced_sync"`, `candidate_count=0`) —
    # never a real compacting pass — i.e. spill, not compaction, is what
    # actually let the retry succeed.
    checks = [e for e in events if e.type == "compaction_check"]
    assert all(
        e.data.get("outcome") == "forced_sync_no_turns"
        or (e.data.get("outcome") == "forced_sync" and e.data.get("candidate_count") == 0)
        for e in checks
    ), (
        f"a real compaction ran for a single-oversized-result overflow — "
        f"spill alone should have sufficed: {[e.data for e in checks]!r}"
    )
    assert not [e for e in events if e.type == "compaction_completed"], (
        "no compaction pass should have actually completed"
    )
    reduced = [e for e in events if e.type == "payload_reduced"]
    assert reduced, "expected a payload_reduced event"
    assert reduced[0].data.get("chain_id") == "c1"
    assert reduced[0].data.get("attempt") == 1

    # The huge string must no longer appear verbatim in what the loop was
    # LAST handed — it was replaced by a bounded preview.
    last_call = loop.calls[-1]
    assert not _has_content(last_call, huge)


# ── ① small turns in bulk — spill finds nothing, compaction closes it ───────


def test_history_dominant_overflow_recovers_via_compaction_not_spill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: contract acceptance ① — many small turns, no tool-result
    turn at all (nothing spillable — spill's own candidate scan
    structurally finds zero candidates), so compaction must be what
    actually recovers the turn.

    ``fake_compaction_engine`` (a network-free stand-in, same shape the
    PR-N6 test file's own ``_OverflowingEngine`` uses — a real
    ``CompactionEngine.compact()`` would attempt an actual litellm call)
    declares FIXED, small budgets (``head_budget=1000``,
    ``tail_budget=1500``, ``effective_trigger=7000``) — 50 turns of 80
    tokens each (4000 total, measured directly while building this test)
    exceeds ``head_budget + tail_budget`` (real compaction candidates
    exist for ``force_compact_now``) while staying under
    ``effective_trigger`` (``decompose_history_for_retry`` keeps
    ``raw_middle`` EMPTY — retry_loop's own internal fold never
    triggers, so this genuinely exercises THIS wrapper's own
    ``_attempt_compaction_reduction`` fallback, not a pre-existing
    mechanism)."""
    session = _make_spill_session(
        tmp_path, monkeypatch, max_shrink_iterations=1, fake_compaction_engine=True,
    )
    for _i in range(50):
        _push(session, "user", "X" * 320)

    events: list = []
    # The REAL, observable signal that compaction (not spill) is what
    # unblocked this turn: a `compaction_completed` event — driven off
    # whichever path actually retires content (retry_loop's own internal
    # raw_middle fold, or this wrapper's own `force_compact_now` fallback
    # both emit the SAME event; acceptance ① only cares that compaction —
    # not spill — is what did it).
    compacted = {"done": False}

    def _on_event(e) -> None:
        events.append(e)
        if e.type == "compaction_completed":
            compacted["done"] = True

    session._audit_events.add_subscriber(_on_event)

    loop = _ContentDrivenLoop(lambda history, user_text: not compacted["done"])

    result = asyncio.run(
        session._loop_driver._run_with_shrink_and_byte_reduction(
            loop, "continue please", chain_id="c1",
        )
    )
    assert result is None

    assert compacted["done"], "expected a real compaction_completed event"
    spill_events = [e for e in events if e.type == "tool_result_offloaded"]
    assert not spill_events, (
        "nothing was spillable (no tool-result turns) — spill must not "
        f"have offloaded anything: {[e.data for e in spill_events]!r}"
    )


# ── ③ oversized user message alone — both levers fail, clean termination ───


def test_oversized_new_message_alone_terminates_cleanly_not_a_hang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: contract acceptance ③ — the incoming user message ITSELF
    (never spilled, never compacted — #5296's own contract, and this
    module's #43-cited "NEVER dropped" invariant) is what is oversized.
    Neither spill nor compaction can touch it, so the wrapper must
    terminate — bounded by construction (`_MAX_BYTE_REDUCTION_ATTEMPTS`),
    not by a wall-clock timeout this test would have to wait out."""
    session = _make_spill_session(tmp_path, monkeypatch, max_shrink_iterations=1)
    _push(session, "user", "hi")
    _push(session, "tool", "small result", tool_call_id="tc1", name="tool")
    _push(session, "assistant", "ok")

    loop = _ContentDrivenLoop(lambda history, user_text: True)  # always 413

    with pytest.raises(UnrecoveredError) as excinfo:
        asyncio.run(
            session._loop_driver._run_with_shrink_and_byte_reduction(
                loop, "X" * 1_000_000, chain_id="c1",
            )
        )
    assert excinfo.value.saw_byte_limit is True

    # Bounded: (1 + _MAX_BYTE_REDUCTION_ATTEMPTS) outer attempts, each an
    # independent retry_loop call bounded by max_shrink_iterations=1 (one
    # main_call per outer attempt here — no raw_middle to fold first).
    from reyn.runtime.services.router_loop_driver import _MAX_BYTE_REDUCTION_ATTEMPTS
    assert len(loop.calls) <= (1 + _MAX_BYTE_REDUCTION_ATTEMPTS) * 1


# ── spill persists across turns (session-lived overlay) ────────────────────


def test_spill_persists_into_the_next_turn_413_fires_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: contract acceptance — 2 turns in a row that would BOTH
    naturally 413 on the same oversized tool result: turn 1 recovers via
    spill; turn 2, hitting the SAME still-inline-in-self.history turn,
    must NOT need to recover again — the overlay persists (session-lived,
    #5296's own architect ruling) so `_serialise_turn` already projects
    the spilled form on turn 2's very first attempt. Witnessed via the
    fake loop's own call count for turn 2 (== 1, no 413 at all) AND via
    the real on-disk manifest (the spill is not merely an in-memory
    claim)."""
    session = _make_spill_session(tmp_path, monkeypatch)
    _push(session, "user", "look something up")
    huge = "Q" * 50_000
    _push(session, "tool", huge, tool_call_id="tc1", name="tool")
    _push(session, "assistant", "ok, done")

    loop1 = _ContentDrivenLoop(lambda history, user_text: _has_content(history, huge))
    asyncio.run(
        session._loop_driver._run_with_shrink_and_byte_reduction(
            loop1, "continue please", chain_id="c1",
        )
    )
    assert any(_has_content(c, huge) for c in loop1.calls[:-1]), (
        "control arm: turn 1 must have actually hit the 413 at least once "
        "before recovering, else this test cannot witness a difference"
    )

    manifest_path = session._media_store._spill_manifest_path()
    assert manifest_path.is_file() and manifest_path.stat().st_size > 0, (
        "spill must be recorded in the real on-disk manifest, not just "
        "the in-memory overlay"
    )

    # Turn 2: the SAME history (spill did not touch self.history) — the
    # overlay from turn 1 must already apply.
    loop2 = _ContentDrivenLoop(lambda history, user_text: _has_content(history, huge))
    asyncio.run(
        session._loop_driver._run_with_shrink_and_byte_reduction(
            loop2, "one more thing", chain_id="c2",
        )
    )
    assert loop2.calls, "control arm: turn 2 must have called loop.run at least once"
    assert loop2.calls[1:] == [], (
        f"turn 2 must succeed on its FIRST attempt (overlay already "
        f"applied) — observed {len(loop2.calls)} calls"
    )
