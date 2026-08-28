"""Tier 2: #5296 PR-2 / #5364 §1.6 — same-turn recovery from an
`UnrecoveredError`, MODE-INDEPENDENT (byte-limited HTTP 413 OR a non-byte,
token-axis terminal cause).

Before #5296 PR-2, `RouterLoopDriver._run_with_shrink`'s own
`UnrecoveredError` always ended the turn — #4954(b)'s own
`recovery_policy="next_turn"` only advanced the watermark for a LATER turn
via a real `force_compact_now()`, but THIS turn still failed. #5296 PR-2's
`_run_with_shrink_and_byte_reduction` (the new wrapper `run_turn` now calls
instead of the bare `_run_with_shrink`) intervenes on exactly that failure
shape: spill (reusing the existing `MediaStore.save_tool_result` +
`tool_result_cap.cap_tool_result_content` machinery via a new session-lived,
non-durable `RouterHistoryBuffer` projection overlay — never
`self.history`/`history.jsonl`, never the compaction watermark), then
re-tries `_run_with_shrink`.

#5364 §1.6 replaces PR-2's original byte-comparison gate and fixed
`_MAX_BYTE_REDUCTION_ATTEMPTS` cap (both removed entirely) with 3
predicates, from the #5364 issue body (canonical): PROGRESS — the
un-spilled candidate count went down by one (the sole termination
witness); SUCCESS — the retried call stops raising; FAILURE — candidates
are empty. The bound is candidate exhaustion, never a fixed constant, and
applies identically whether `UnrecoveredError.saw_byte_limit` is True or
False — a token-axis cause now gets the same spill attempts a byte-limit
cause always did.

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
    fake_compaction_engine: bool = False, recovery_policy: str = "next_turn",
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
    others never let ``compact()`` run for real at all.

    ``recovery_policy`` — lead-coder review: default ``"next_turn"`` means
    ``_run_with_shrink``'s own PRE-EXISTING #4954(b) side-effect ALSO
    compacts on every byte-limited failure, confounding a test that wants
    to isolate whether THIS PR's own ``_attempt_compaction_reduction`` is
    what recovered a turn (measured directly: with the pre-existing
    side-effect left on, disabling this PR's compaction call entirely
    still passed the "compaction recovers it" scenario — the pre-existing
    mechanism alone was doing the work, and the test never noticed).
    ``"never"`` disables that side-effect, so any compaction observed can
    only be THIS PR's own."""
    monkeypatch.chdir(tmp_path)
    if t_max is not None:
        import reyn.llm.model_budget as _mb
        monkeypatch.setattr(_mb, "get_max_input_tokens", lambda model, **kw: t_max)
    cfg = CompactionConfig(
        body_token_cap=1500,
        use_chars4_estimate=True,
        section_caps_spec_tokens=0,
        max_shrink_iterations=max_shrink_iterations,
        recovery_policy=recovery_policy,
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


# ── ① small turns in bulk — spill finds nothing; recovery depends on the ───
# ── PRE-EXISTING next_turn side-effect, which this wrapper must detect ─────


def test_history_dominant_overflow_recovers_via_pre_existing_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: contract acceptance ① — many small turns, no tool-result
    turn at all (nothing spillable — spill's own candidate scan
    structurally finds zero candidates). ★architect review — the FIRST
    version of #5364 §1.6 broke this: it read only the spill axis's own
    progress, so "spill candidates ZERO" was (wrongly) treated as
    "nothing left to try" even though the COMPACT axis
    (``_run_with_shrink``'s own PRE-EXISTING #4954(b) ``next_turn``
    side-effect, ``force_compact_now`` inside ITS except block) had
    genuinely advanced the watermark THIS attempt and let the SECOND
    ``_run_with_shrink`` call succeed — a real, witnessed, main-green
    recovery path, not a theoretical one (this test's own docstring, pre-
    fix, already said so verbatim). #5367's own "縮小軸は2本／失敗＝両縮小
    軸が dry" already named this; #5364 §1.6 only wrote the spill side —
    an omission in that text, not in the pre-#5364-§1.6 implementation.
    Fixed by tracking BOTH axes' progress (``compaction_watermark()``
    strictly increasing = compact progress, alongside the pre-existing
    spill-candidate-consumed signal) and failing only when NEITHER moved.

    ``fake_compaction_engine`` (a network-free stand-in, same shape the
    PR-N6 test file's own ``_OverflowingEngine`` uses — a real
    ``CompactionEngine.compact()`` would attempt an actual litellm call)
    declares FIXED, small budgets (``head_budget=1000``,
    ``tail_budget=1500``, ``effective_trigger=7000``) — 50 turns of 80
    tokens each (4000 total, measured directly while building this test)
    exceeds ``head_budget + tail_budget`` (real compaction candidates
    exist) while staying under ``effective_trigger``
    (``decompose_history_for_retry`` keeps ``raw_middle`` EMPTY —
    retry_loop's own internal fold never triggers, isolating the
    pre-existing except-block side-effect as the only compaction path
    exercised)."""
    session = _make_spill_session(
        tmp_path, monkeypatch, max_shrink_iterations=1,
        fake_compaction_engine=True,  # recovery_policy="next_turn" (default)
    )
    for _i in range(50):
        _push(session, "user", "X" * 320)

    events: list = []
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
    assert result is None  # the fake loop's own successful return

    assert compacted["done"], "expected a real compaction_completed event"
    spill_events = [e for e in events if e.type == "tool_result_offloaded"]
    assert not spill_events, (
        "nothing was spillable (no tool-result turns) — spill must not "
        f"have offloaded anything: {[e.data for e in spill_events]!r}"
    )


def test_history_dominant_overflow_fails_fast_with_nothing_spillable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: contract acceptance ①b — the TRUE-exhaustion sibling of
    the test above: nothing spillable (no tool-result turns) AND
    compaction ALSO cannot progress (too few durable turns for
    ``force_compact_now``'s own candidate scan to find anything —
    ``fake_compaction_engine``'s ``compact()`` never even gets called).
    Together the two tests distinguish "one axis dry" (recovers) from
    "both axes dry" (true exhaustion, #5364 §1.6's unqualified failure
    predicate: raise immediately, no generous extra attempts)."""
    session = _make_spill_session(
        tmp_path, monkeypatch, max_shrink_iterations=1,
        fake_compaction_engine=True,  # recovery_policy="next_turn" (default)
    )
    _push(session, "user", "hi")
    _push(session, "assistant", "ok")

    events: list = []
    session._audit_events.add_subscriber(lambda e: events.append(e))

    loop = _ContentDrivenLoop(lambda history, user_text: True)  # always 413

    with pytest.raises(UnrecoveredError) as excinfo:
        asyncio.run(
            session._loop_driver._run_with_shrink_and_byte_reduction(
                loop, "continue please", chain_id="c1",
            )
        )
    assert excinfo.value.saw_byte_limit is True

    assert not [e for e in events if e.type == "compaction_completed"], (
        "too little history for compaction to have found any candidate — "
        "the watermark must NOT have moved (control arm: if this test's "
        "own compaction axis silently moved, the test is not actually "
        "isolating the true-exhaustion case it means to)"
    )
    spill_events = [e for e in events if e.type == "tool_result_offloaded"]
    assert not spill_events, (
        "nothing was spillable (no tool-result turns) — spill must not "
        f"have offloaded anything: {[e.data for e in spill_events]!r}"
    )


def test_recovery_policy_never_leaves_the_watermark_alone_and_terminates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5296 PR-2 review (architect's own prescribed witness,
    2nd finding) — with ``recovery_policy="never"``, an operator's
    "don't summarize my history" choice must hold even through THIS
    wrapper's own retry path: the SAME history-dominant, nothing-
    spillable scenario as the sibling test above, but with compaction
    disabled, must (①) leave the compaction watermark exactly where it
    was (no ``compaction_completed`` at all — not the pre-existing
    side-effect, and not a resurrected wrapper-owned call) and (②)
    terminate cleanly (raise ``UnrecoveredError``, not hang or loop
    forever) rather than silently compacting anyway."""
    session = _make_spill_session(
        tmp_path, monkeypatch, max_shrink_iterations=1,
        fake_compaction_engine=True, recovery_policy="never",
    )
    for _i in range(50):
        _push(session, "user", "X" * 320)

    events: list = []
    session._audit_events.add_subscriber(lambda e: events.append(e))

    loop = _ContentDrivenLoop(lambda history, user_text: True)  # always 413

    with pytest.raises(UnrecoveredError) as excinfo:
        asyncio.run(
            session._loop_driver._run_with_shrink_and_byte_reduction(
                loop, "continue please", chain_id="c1",
            )
        )
    assert excinfo.value.saw_byte_limit is True

    # ① watermark untouched.
    assert not [e for e in events if e.type == "compaction_completed"], (
        "recovery_policy='never' must leave the watermark alone — no "
        "compaction_completed event may fire, from either the pre-"
        "existing next_turn side-effect (disabled by this policy) or a "
        "wrapper-owned call (removed entirely)"
    )
    # ② clean, bounded termination — not a hang. #5364 §1.6: the bound is
    # candidate exhaustion, not a fixed constant. All 50 turns here are
    # ``role="user"`` — zero spillable candidates — so
    # ``_attempt_reactive_spill`` reports failure on its very FIRST call,
    # and the wrapper must raise right after exactly ONE outer attempt (a
    # wall-clock timeout is never needed to prove this): sliced past that
    # single attempt's own ``_run_with_shrink`` call count (2, measured
    # directly — ``max_shrink_iterations=1`` plus retry_loop's own initial
    # full-history attempt, unrelated to and unchanged by this fix), the
    # tail must be empty.
    assert loop.calls[2:] == []


# ── ③ oversized user message alone — both levers fail, clean termination ───


def test_oversized_new_message_alone_terminates_cleanly_not_a_hang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: contract acceptance ③ — the incoming user message ITSELF
    (never spilled, never compacted — #5296's own contract, and this
    module's #43-cited "NEVER dropped" invariant) is what is oversized.
    Neither spill nor compaction can touch it, so the wrapper must
    terminate — #5364 §1.6: bounded by candidate exhaustion (one genuine
    spill of the single small tool candidate, then failure once that
    candidate is already at its own offload floor), never by a fixed
    constant or a wall-clock timeout this test would have to wait out."""
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

    # Bounded: 2 outer attempts, each an independent retry_loop call —
    # attempt 1 spills the single small tool candidate (genuine
    # progress), attempt 2 finds that same candidate already at its own
    # offload floor (no progress, #5364 §1.6's ``is_already_spilled``
    # guard) and raises. Each outer attempt's own ``_run_with_shrink``
    # call makes 2 loop.run calls (measured directly — max_shrink_
    # iterations=1's own initial full-history attempt plus one more,
    # unrelated to and unchanged by this fix), so the composite bound is
    # 4 total; sliced past it, the tail must be empty.
    assert loop.calls[4:] == []


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

    async def _turn1() -> None:
        await session._loop_driver._run_with_shrink_and_byte_reduction(
            loop1, "continue please", chain_id="c1",
        )
        # #5364 §1.4: the manifest append is now off-loop (fire-and-forget,
        # chained after the content write on save_tool_result's own
        # worker — see media_store.py's own comment on that ordering). A
        # REAL turn's own retry always re-enters RouterLoop.run_loop
        # before its next LLM call, which flushes this durable —
        # _ContentDrivenLoop (this test's own docstring: "A fake
        # RouterLoop") never does, so this test needs the same explicit
        # flush a real turn gets for free. MUST run in the SAME
        # asyncio.run (DurabilityWorker.flush() no-ops on a different
        # loop than the one its queue is bound to — see its own guard).
        await session._media_store.flush()

    asyncio.run(_turn1())
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


# ── a genuine spill is KEPT even when it makes the payload bigger ──────────


def test_a_spill_that_makes_it_bigger_is_still_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5364 §1.6 (reverses #5296 PR-2's original 1st finding) —
    spilling a TINY tool result makes it BIGGER, not smaller (the
    offloaded preview carries a fixed pointer-path overhead — measured
    directly building #5296 PR-2: an 11-char original became a 115-char
    replacement). #5296 PR-2 originally UNDID such a spill
    (``discard_spill_overlay_for``); #5364 §1.6 removes that undo
    entirely — the stop condition is "did progress happen" (a candidate
    got consumed), never "did bytes move", so a genuine new spill is now
    KEPT regardless of whether it made the payload bigger. Witnessed via
    the PUBLIC ``build_history()`` seam (never the private
    ``_spill_overlay`` dict directly): the tiny turn's OFFLOADED preview
    (not its original content) is what gets sent afterward."""
    session = _make_spill_session(tmp_path, monkeypatch)
    _push(session, "user", "hi")
    # NOT "hi" — a 2-char body estimates at <=1 token, so `cap_tokens=1`
    # never offloads it at all (`spill_turn_content` returns `None`,
    # not a genuine spill). "tiny result" (11 chars) DOES cross the
    # 1-token cap and genuinely gets offloaded — into a 115-char
    # preview, bigger than the original.
    tiny = "tiny result"
    _push(session, "tool", tiny, tool_call_id="tc1", name="tool")
    _push(session, "assistant", "ok")

    progressed = asyncio.run(
        session._loop_driver._attempt_reactive_spill(chain_id="c1")
    )
    assert progressed is True, (
        "spilling a tiny result IS progress (a candidate was genuinely "
        "newly offloaded) even though the replacement is bigger than "
        "the original — #5364 §1.6 never reads bytes to decide this"
    )

    history = session._loop_driver._history_buffer.build_history()
    assert not _has_content(history, tiny), (
        "the spill must be KEPT, not undone — the tiny turn's ORIGINAL "
        "content must no longer be what gets sent"
    )
    assert "read_file(path=" in str(history), (
        "the offloaded preview (naming a read_file path) must be what "
        "gets sent in place of the tiny turn's original content"
    )


# ── #5364: candidate order is STAGED (head → mid → tail), size-desc/stage ──


@pytest.mark.asyncio
async def test_spill_candidates_are_staged_head_then_mid_then_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5364 §1.3 (owner verbatim "mid も対象にしてね。head->mid->
    tail->open") — the FIRST candidate spilled is a ``head`` turn, never a
    ``raw_middle`` turn, even when the mid candidate's content dwarfs
    every head candidate. A global size-sort (the bug this staged design
    replaces) would offer the largest content first regardless of group —
    here that would be the mid candidate — so which one is spilled FIRST
    directly distinguishes staged order from global size order.

    Witnessed via the PUBLIC ``tool_result_offloaded`` audit-event
    (``tool_result_cap.py``'s own emit, threaded through
    ``spill_turn_content``) — its ``total_chars`` names the size of the
    candidate that was JUST spilled, a public order-witness for
    ``RouterLoopDriver._spill_candidates`` without calling that private
    static method directly (CLAUDE.md testing policy: "if neither [a
    public surface nor a snapshot-style read] exists, that absence is the
    finding" — here a public read DOES exist once driven through a real
    spill pass, so this is that seam, not a documented absence).
    ``session._audit_events.drain()`` is required before reading the
    subscriber's list — events queue, they are not delivered
    synchronously inside ``emit()`` (measured directly: the subscriber
    list was empty without it, on every run)."""
    session = _make_spill_session(tmp_path, monkeypatch, t_max=2_500)
    events: list = []
    session._audit_events.add_subscriber(lambda e: events.append(e))

    # Head: one small tool turn. Mid: one turn whose content is FAR
    # larger than the head candidate — if global size-sort fired instead
    # of staged order, THIS would be spilled first. Padding filler turns
    # (not tool-role, never candidates) so t_max forces a genuine
    # head/mid split with each tool turn landing in its own group.
    small_head_content = "tiny result h1 " + "a" * 10
    huge_mid_content = "M" * 5_000
    _push(session, "tool", small_head_content, tool_call_id="tc-h1", name="tool")
    for i in range(20):
        _push(session, "user", f"filler question number {i + 100} " * 8)
        _push(session, "assistant", f"filler answer number {i + 100} " * 8)
    _push(session, "tool", huge_mid_content, tool_call_id="tc-m1", name="tool")
    for i in range(3):
        _push(session, "user", f"filler question number {i + 300} " * 8)
        _push(session, "assistant", f"filler answer number {i + 300} " * 8)

    head, raw_middle, _tail, _summary, _seq_by_id = (
        session._loop_driver._history_buffer.decompose_history_for_retry()
    )
    head_ids = {t.get("tool_call_id") for t in head if t.get("role") == "tool"}
    mid_ids = {t.get("tool_call_id") for t in raw_middle if t.get("role") == "tool"}
    assert head_ids == {"tc-h1"}, (
        f"test setup sanity: the head candidate must land in head, got {head_ids!r} "
        f"— adjust t_max/turn counts"
    )
    assert mid_ids == {"tc-m1"}, (
        f"test setup sanity: the mid candidate must land in raw_middle, got {mid_ids!r}"
    )

    await session._loop_driver._attempt_reactive_spill(chain_id="c1")
    await session._audit_events.drain()

    offloaded = [e for e in events if e.type == "tool_result_offloaded"]
    assert offloaded, "test setup sanity: at least one candidate must have been spilled"
    first_spilled_size = offloaded[0].data["total_chars"]
    assert first_spilled_size == len(small_head_content), (
        f"the FIRST candidate spilled must be the head turn "
        f"({len(small_head_content)} chars), not the far-larger mid turn "
        f"({len(huge_mid_content)} chars) — got first_spilled_size="
        f"{first_spilled_size}. A global size-sort would spill the mid "
        f"candidate first."
    )


class _SpillableByteLimitMidEngine:
    """Real-shaped ``CompactionEngine`` stand-in whose ``compact()`` 413s
    while the offered slice's FIRST turn still carries the ORIGINAL
    marker content, succeeds once it is the SPILLED content — the driver-
    path witness that ``RouterLoopDriver``'s own ``spill_fn`` wiring (not
    merely ``retry_loop``'s internal logic, already covered directly in
    ``test_pr_n6_compaction_overflow_retry.py``) is what makes this
    resolve. ``raw_middle[0]`` is always the offered slice's first turn
    regardless of how far ``_compact_attempt_len`` has halved (the slice
    is always ``raw_middle[:_attempt_len]``, taken from index 0), so
    checking only ``new_turns[0]`` is sufficient here."""

    def __init__(self) -> None:
        from reyn.core.events.events import EventLog
        from reyn.services.compaction.engine import ComputedBudgets
        self.budgets = ComputedBudgets(
            main_pool=10_000, head_budget=20, body_budget=500,
            tail_budget=20, new_msg_budget=1_000,
            B_M=8_000, main_M_room=7_000, effective_trigger=3_000,
            section_caps={
                "topic_arc": 50, "decisions": 200, "pending": 150,
                "session_user_facts": 50, "artifacts_referenced": 175,
            },
        )
        self._events = EventLog()
        self._T_comp_SP = 100
        self._model = "openai/test-standard-model"
        self.compact_calls = 0

    async def compact(self, input_chunk):
        self.compact_calls += 1
        turn = input_chunk.new_turns[0]
        content = turn.get("content") if isinstance(turn, dict) else None
        if content == "OVERSIZED_MARKER_5367_3":
            raise _FakeStatusError("compact 413", status_code=413)
        from reyn.services.compaction.engine import ChatSummary

        def _seq(t: object) -> int:
            return t.get("seq", 0) if isinstance(t, dict) else getattr(t, "seq", 0)

        return ChatSummary(
            topic_arc="ok", covers_through_seq=max((_seq(t) for t in input_chunk.new_turns), default=0),
        )


def test_run_with_shrink_wires_spill_fn_into_retry_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5367③ BLOCKING① witness — a REAL driver path
    (``RouterLoopDriver._run_with_shrink``, real ``Session``/
    ``RouterHistoryBuffer``/``MediaStore``) resolves a byte-limit
    mid-split-floor overflow via the ``spill_fn`` THIS PR wires in,
    not merely ``retry_loop``'s own internal logic (already covered
    directly in ``test_pr_n6_compaction_overflow_retry.py``).

    Strip-falsify: removing ``spill_fn=_spill_fn,`` from
    ``router_loop_driver.py``'s ``_retry_loop(...)`` call makes this test
    raise ``UnrecoveredError`` instead of returning — ``retry_loop``
    receives ``spill_fn=None`` and its own ``if spill_fn is None or not
    raw_middle: return False`` guard makes the new mechanism a silent
    no-op, exactly BLOCKING①'s point (a test that calls ``retry_loop``
    directly and supplies its own ``spill_fn=`` cannot catch this — only
    a test that goes through the real caller can).

    The injected ``_SpillableByteLimitMidEngine`` carries its OWN tiny
    budgets (``effective_trigger=3_000``, ``head_budget``/``tail_budget``
    ``=20``) — ``resolve_effective_trigger_and_budgets`` reads these off
    the compaction controller's cached engine, not from ``t_max``, once an
    engine is injected (measured directly while building this test:
    ``t_max`` alone left everything in ``head`` regardless of its value).
    One small head turn + the marker (tool) + 7 filler (user, assistant)
    pairs reliably lands the marker turn alone as ``raw_middle[0]`` — sized
    in spirit only (the marker's actual content is tiny; only WIRING is
    this test's subject, not byte-size behavior, which the engine-level
    tests in ``test_pr_n6_compaction_overflow_retry.py`` already cover).
    ``max_shrink_iterations=25`` is generous: the halving ladder needs a
    few attempts to reach the mid=1 floor, then (after the spill succeeds)
    retry_loop folds the remaining filler turns one at a time before ever
    reaching ``main_call`` — a real, if wasteful, consequence of #4947 ③'s
    "don't reset the discovered slice size to full" choice, not a bug this
    test is pinning."""
    session = _make_spill_session(
        tmp_path, monkeypatch, t_max=2_500, max_shrink_iterations=25,
        recovery_policy="never",
    )
    session._compaction_controller._CompactionController__engine_cache = (
        _SpillableByteLimitMidEngine()
    )
    _push(session, "user", "small head content " * 5)
    _push(session, "tool", "OVERSIZED_MARKER_5367_3", tool_call_id="tc-marker", name="big_tool")
    for i in range(7):
        _push(session, "user", f"filler question number {i} " * 40)
        _push(session, "assistant", f"filler answer number {i} " * 40)

    head, raw_middle, _tail, _summary, _seq_by_id = (
        session._loop_driver._history_buffer.decompose_history_for_retry()
    )
    mid_ids = {t.get("tool_call_id") for t in raw_middle if t.get("role") == "tool"}
    assert mid_ids == {"tc-marker"}, (
        f"test setup sanity: the marker turn must land alone in "
        f"raw_middle's tool turns, got {mid_ids!r} — adjust t_max/turn "
        f"placement (this mirrors test_retry_loop_chat_wiring_1125.py's "
        f"own independently-measured t_max=2800/8-turn split)"
    )
    assert raw_middle[0].get("tool_call_id") == "tc-marker", (
        "test setup sanity: the marker turn must be raw_middle[0] — the "
        "halving ladder always offers raw_middle[:_attempt_len] from "
        "index 0"
    )

    # The FIRST call (via build_history()) must fail unconditionally to
    # enter retry_loop at all — build_history's own elide logic already
    # hides raw_middle's content from the wire before any real overflow
    # occurs (this scenario's content is elidable-away by construction),
    # so a marker-presence check alone would never see the first call
    # fail. Every call AFTER the first goes through retry_loop's own
    # internal main_call (head+summary+tail only, never raw_middle), so
    # once compact() succeeds on the spilled content, that call's payload
    # genuinely no longer carries the marker either way — checking call
    # ORDER (first vs. later), not payload shape, is what this predicate
    # actually needs.
    _seen_first_call = {"done": False}

    def _fail_only_the_very_first_call(history: list, user_text: str) -> bool:
        if _seen_first_call["done"]:
            return False
        _seen_first_call["done"] = True
        return True

    loop = _ContentDrivenLoop(_fail_only_the_very_first_call)

    # No exception raised (the assertion is the ABSENCE of one — retry_loop
    # only returns via the fake loop's OWN return value, None on success,
    # matching the sibling test's convention).
    asyncio.run(
        session._loop_driver._run_with_shrink(
            loop, "continue please", chain_id="c1",
        )
    )
    engine = session._compaction_controller._CompactionController__engine_cache
    assert engine.compact_calls >= 2, (
        f"expected at least 2 compact() calls (failing attempts on the "
        f"original marker content, then a succeeding one on the spilled "
        f"content) — got {engine.compact_calls}, meaning the spilled "
        f"content never reached engine.compact() at all"
    )


@pytest.mark.asyncio
async def test_spill_turn_content_offload_event_names_trigger_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5367①/BLOCKING witness — the REAL production wiring
    (``RouterHistoryBuffer.spill_turn_content``, driven through
    ``RouterLoopDriver._attempt_reactive_spill``, no fake collaborator)
    names its ``tool_result_offloaded`` event's ``trigger`` as ``"overflow"``.

    Strip-falsify: swapping ``TRIGGER_CAP``/``TRIGGER_OVERFLOW`` in
    ``router_history_buffer.py`` turns this RED (the event would carry
    ``"cap"`` instead)."""
    session = _make_spill_session(tmp_path, monkeypatch, t_max=2_500)
    events: list = []
    session._audit_events.add_subscriber(lambda e: events.append(e))

    _push(session, "tool", "huge tool result " + "z" * 5_000, tool_call_id="tc-1", name="tool")
    for i in range(20):
        _push(session, "user", f"filler question number {i} " * 8)
        _push(session, "assistant", f"filler answer number {i} " * 8)

    await session._loop_driver._attempt_reactive_spill(chain_id="c1")
    await session._audit_events.drain()

    offloaded = [e for e in events if e.type == "tool_result_offloaded"]
    assert offloaded, "test setup sanity: at least one candidate must have been spilled"
    assert offloaded[0].data["trigger"] == "overflow"


@pytest.mark.asyncio
async def test_a_mid_spill_is_kept_even_though_it_moves_zero_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5364 §1.3/§1.6 — a ``raw_middle`` candidate can never move
    wire bytes (elided out of ``estimate_wire_bytes`` by construction), so
    the stop condition must NOT read bytes at all — it must be "did
    progress happen" (a candidate got consumed), never "did bytes move".
    Reading bytes here would discard every mid spill outright, contradicting
    #5364's own reason for including mid at all (persisted ``spilled``
    state + a smaller future compaction fold). This setup has exactly ONE
    spillable candidate, and it lands in ``raw_middle`` alone (no head/tail
    tool turns to spill first), isolating the mid-only case. Witnessed via
    ``decompose_history_for_retry()``'s own returned content (never the
    private ``_spill_overlay`` dict directly) — surviving content must
    equal the offload preview, matched by its ``read_file(path=...)``
    marker."""
    session = _make_spill_session(tmp_path, monkeypatch, t_max=2_500)
    # Filler BEFORE and after the sole tool turn — enough that it lands
    # squarely in raw_middle (never head/tail) once elided, mirroring
    # test_spill_candidates_are_staged_head_then_mid_then_tail's own
    # independently-measured t_max=2_500/filler shape. No OTHER tool
    # turns exist, so this is the sole candidate.
    for i in range(20):
        _push(session, "user", f"filler question number {i + 100} " * 8)
        _push(session, "assistant", f"filler answer number {i + 100} " * 8)
    _push(session, "tool", "result body " * 100, tool_call_id="tc-mid", name="tool")
    for i in range(3):
        _push(session, "user", f"filler question number {i + 300} " * 8)
        _push(session, "assistant", f"filler answer number {i + 300} " * 8)

    head, raw_middle, tail, _summary, _seq_by_id = (
        session._loop_driver._history_buffer.decompose_history_for_retry()
    )
    mid_tools = [t for t in raw_middle if t.get("role") == "tool"]
    assert mid_tools, (
        "test setup sanity: raw_middle must contain at least one tool "
        "turn, or this test cannot exercise the mid-is-never-undone path "
        "— adjust t_max/turn count"
    )
    assert not [t for t in head + tail if t.get("role") == "tool"], (
        "test setup sanity: no head/tail tool candidate must exist, or "
        "this test cannot isolate the mid-only case (a head/tail "
        "candidate would be spilled FIRST, per #5364 §1.3's staged order)"
    )

    progressed = await session._loop_driver._attempt_reactive_spill(chain_id="c1")
    assert progressed is True, (
        "a genuine new mid spill IS progress (a candidate was newly "
        "offloaded), even though it moves zero wire bytes — #5364 §1.6 "
        "never reads bytes to decide this"
    )

    original_mid_content = mid_tools[0]["content"]
    target_id = mid_tools[0].get("tool_call_id")
    head2, raw_middle2, tail2, _summary2, _seq2 = (
        session._loop_driver._history_buffer.decompose_history_for_retry()
    )
    reserialised = [
        t for t in head2 + raw_middle2 + tail2
        if t.get("tool_call_id") == target_id
    ]
    assert reserialised, "test setup sanity: the same mid turn must still decompose somewhere"
    assert reserialised[0]["content"] != original_mid_content, (
        "the mid spill must survive into a LATER decompose_history_for_retry() "
        "call — its content should now be the offloaded preview, not the "
        "original body. If this is still the original content, the "
        "overlay was discarded (the bug #5364 names: mid spills undone "
        "because they never satisfy 'after < before')."
    )
    assert "read_file(path=" in reserialised[0]["content"], (
        "the surviving content should be the offload preview naming the "
        f"read-back path — got: {reserialised[0]['content']!r}"
    )


# ── #5364 §1.6 REQUIRED acceptance: mid-only, multi-round, bytes constant ──


def test_mid_only_spill_bounds_by_candidate_exhaustion_wire_bytes_never_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5364 §1.6 REQUIRED acceptance — a mid-only-candidates
    configuration (3 spillable ``raw_middle`` tool turns, nothing in
    head/tail) where spill continues across multiple retries, each one
    strictly decreasing the un-spilled candidate count by exactly one
    (PROGRESS), main-call wire bytes stay IDENTICAL throughout every
    retry (mid is elided out of ``estimate_wire_bytes`` by construction
    — #5364 §1.3), and the turn ultimately SUCCEEDS once every candidate
    has been spilled (the retried call stops raising — #5364 §1.6's own
    SUCCESS predicate, never a separate byte-target comparison).

    Strip-falsify (disclosed, not re-run inline — the old code no longer
    exists to revert to): reverting ``_attempt_reactive_spill`` to the
    OLD ``self._wire_bytes_now() >= attempt_start_bytes: raise`` stop
    condition makes this test go RED on the very FIRST round — that
    check reads "mid spill moved zero bytes" as failure and raises
    immediately, before a second of the 3 candidates is ever reached.
    """
    session = _make_spill_session(tmp_path, monkeypatch, t_max=2_500)
    for i in range(3):
        for j in range(8):
            _push(session, "user", f"filler {i}-{j} " * 8)
            _push(session, "assistant", f"filler reply {i}-{j} " * 8)
        _push(session, "tool", f"mid result {i} " * 100, tool_call_id=f"tc-mid{i}", name="tool")
    for i in range(3):
        _push(session, "user", f"tail filler {i} " * 8)
        _push(session, "assistant", f"tail reply {i} " * 8)

    head, raw_middle, tail, _summary, _ = (
        session._loop_driver._history_buffer.decompose_history_for_retry()
    )
    mid_ids = {t.get("tool_call_id") for t in raw_middle if t.get("role") == "tool"}
    assert mid_ids == {"tc-mid0", "tc-mid1", "tc-mid2"}, (
        f"test setup sanity: all 3 candidates must land in raw_middle "
        f"alone, got {mid_ids!r} — adjust t_max/filler counts"
    )
    assert not [t for t in head + tail if t.get("role") == "tool"], (
        "test setup sanity: no head/tail tool candidate must exist, or "
        "this test cannot isolate the mid-only case"
    )
    raw_middle_chars_before = sum(len(t.get("content", "")) for t in raw_middle)

    def _wire_bytes_of(current_head, current_tail) -> int:
        from reyn.services.compaction.engine import estimate_wire_bytes
        return estimate_wire_bytes(
            SP="sp", head=current_head, summary=None, tail=current_tail,
            new_msg={"role": "user", "content": "continue please"},
        )

    wire_bytes_before = _wire_bytes_of(head, tail)

    spilled_ids: "list[str]" = []
    for _round in range(3):
        progressed = asyncio.run(
            session._loop_driver._attempt_reactive_spill(chain_id="c1")
        )
        assert progressed is True, (
            f"round {_round}: expected progress — {3 - _round} candidate(s) "
            f"should still be un-spilled"
        )
        head_n, raw_middle_n, tail_n, _summary_n, _ = (
            session._loop_driver._history_buffer.decompose_history_for_retry()
        )
        still_original = {
            t.get("tool_call_id") for t in raw_middle_n
            if t.get("role") == "tool" and t.get("tool_call_id") not in spilled_ids
            and "read_file(path=" not in t.get("content", "")
        }
        newly_spilled = mid_ids - still_original - set(spilled_ids)
        (only_newly_spilled,) = newly_spilled  # raises unless exactly one
        spilled_ids.append(only_newly_spilled)
        assert _wire_bytes_of(head_n, tail_n) == wire_bytes_before, (
            f"round {_round}: main-call wire bytes must stay unchanged — "
            f"a mid-only spill can never move them (#5364 §1.3)"
        )

    # All 3 now spilled — candidates exhausted (#5364 §1.6's failure
    # predicate), a natural termination, not a fixed constant.
    final_progressed = asyncio.run(
        session._loop_driver._attempt_reactive_spill(chain_id="c1")
    )
    assert final_progressed is False

    _head_f, raw_middle_f, _tail_f, _summary_f, _ = (
        session._loop_driver._history_buffer.decompose_history_for_retry()
    )
    raw_middle_chars_after = sum(len(t.get("content", "")) for t in raw_middle_f)
    assert raw_middle_chars_after < raw_middle_chars_before, (
        "compaction's own future input (raw_middle) must have SHRUNK once "
        "every mid candidate was spilled into a bounded offload preview"
    )

    # The turn ultimately succeeds: a fake loop that fails until all 3
    # candidates are spilled, driven through the real wrapper end-to-end
    # (a fresh session — the one above already spent its candidates).
    session2 = _make_spill_session(tmp_path, monkeypatch, t_max=2_500)
    for i in range(3):
        for j in range(8):
            _push(session2, "user", f"filler {i}-{j} " * 8)
            _push(session2, "assistant", f"filler reply {i}-{j} " * 8)
        _push(session2, "tool", f"mid result {i} " * 100, tool_call_id=f"tc-mid{i}", name="tool")
    for i in range(3):
        _push(session2, "user", f"tail filler {i} " * 8)
        _push(session2, "assistant", f"tail reply {i} " * 8)
    spilled_so_far = {"n": 0}
    session2._audit_events.add_subscriber(
        lambda e: spilled_so_far.__setitem__("n", spilled_so_far["n"] + 1)
        if e.type == "tool_result_offloaded" else None
    )
    loop2 = _ContentDrivenLoop(lambda history, user_text: spilled_so_far["n"] < 3)
    result = asyncio.run(
        session2._loop_driver._run_with_shrink_and_byte_reduction(
            loop2, "continue please", chain_id="c1",
        )
    )
    assert result is None, "the turn must ultimately succeed once all mid candidates spilled"
    assert spilled_so_far["n"] == 3, (
        "the turn must have spilled all 3 mid candidates before succeeding "
        f"— got {spilled_so_far['n']!r}"
    )
