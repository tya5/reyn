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
from reyn.runtime.chat_message import ChatMessage, Spillability
from reyn.services.compaction.engine import UnrecoveredError
from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle


class _FakeStatusError(Exception):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


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
    recovery_policy: str = "next_turn", spill_granularity: str = "turn",
):
    """A real Session with a real MediaStore (default ``make_session`` gives
    ``media_store=None`` — the spill mechanism needs a real one to have any
    effect at all). ``t_max`` (mirrors ``test_retry_loop_chat_wiring_1125.
    py``'s own harness) forces a small ``effective_trigger`` so a real
    history genuinely produces compaction candidates instead of fitting
    comfortably under the real (fallback ~128k-token) model window.

    #5382: the ``CompactionController``'s engine is ALWAYS the real
    ``CompactionEngine`` now — construction is offline (model-string
    resolution + local token estimation, no litellm call; confirmed by
    architect/lead-coder before this migration), so no fake stand-in is
    needed just to build a session. A test whose OWN scenario reaches an
    actual ``compact()`` call must mark itself ``@pytest.mark.llm_stub``
    (optionally ``raise_for="compaction", cause=...``) — see
    ``reyn.dev.testing.llm_stub``'s own module docstring.

    ``recovery_policy`` — lead-coder review: default ``"next_turn"`` means
    ``_run_with_shrink``'s own PRE-EXISTING #4954(b) side-effect ALSO
    compacts on every byte-limited failure, confounding a test that wants
    to isolate whether THIS PR's own ``_attempt_compaction_reduction`` is
    what recovered a turn (measured directly: with the pre-existing
    side-effect left on, disabling this PR's compaction call entirely
    still passed the "compaction recovers it" scenario — the pre-existing
    mechanism alone was doing the work, and the test never noticed).
    ``"never"`` disables that side-effect, so any compaction observed can
    only be THIS PR's own.

    ``spill_granularity`` (#5592) defaults to ``"turn"`` here — the
    pre-#5592 one-candidate-per-call behavior this whole test file's own
    round-by-round assertions were written against. #5592's own new
    default (``"tier"``, whole-tier-per-call batching) is exercised by
    its own dedicated tests, not by defaulting it on here — changing this
    helper's default would silently re-target every existing test in this
    file at a different mechanism than the one each was written to
    isolate."""
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
        spill_granularity=spill_granularity,
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
    return session


def _has_content(history: "list[dict]", needle: str) -> bool:
    return any(needle in str(m.get("content", "")) for m in history)


# ── ② one huge tool result — spill fixes it, watermark does not move ────────


def test_single_huge_tool_result_recovers_via_spill_not_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: contract acceptance ② — a single oversized tool result is
    the ONLY large thing in history. Spill alone must fix it (watermark
    unchanged — no `compaction_check`/`compaction_completed` event).

    #5622 (PR review, real-execution finding): #5622's own fix made
    retry_loop's internal compact()-call except clause correctly
    classify a genuinely-unrelated exception (this test's own unpinned
    real-network reach, absent a stub) as NOT shrinkable, propagating it
    bare instead of silently absorbing it as a fake OVERFLOW and
    continuing the ladder anyway (the pre-#5622 misclassification bug
    this test was accidentally relying on to survive an otherwise-fatal
    unpinned network reach). ``LLMStub(raise_for=..., cause="byte_limit")``
    forces retry_loop's own compact() to keep failing (a real, byte-
    limited shape) while ANY candidate remains un-spilled — matching
    #5615's own established pattern for isolating "spill resolves it,
    not compact()", never letting compact() opportunistically fold the
    huge result into a summary (a bare, unconditionally-succeeding stub
    does exactly that instead — measured directly, breaking this test's
    own "spill, not compaction" claim)."""
    from reyn.dev.testing.llm_stub import LLMStub

    session = _make_spill_session(tmp_path, monkeypatch)
    _push(session, "user", "look something up")
    huge = "Y" * 50_000
    _push(session, "tool", huge, tool_call_id="tc1", name="tool")
    _push(session, "assistant", "ok, done")

    events = collect_events(session)

    loop = _ContentDrivenLoop(
        lambda history, user_text: _has_content(history, huge)
    )

    stub = LLMStub(raise_for=lambda messages: True, cause="byte_limit")
    stub.install()
    try:
        result = asyncio.run(
            session._loop_driver._run_with_shrink_and_byte_reduction(
                loop, "continue please", chain_id="c1",
            )
        )
    finally:
        stub.restore()
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


@pytest.mark.llm_stub
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

    #5382: was ``fake_compaction_engine`` (a private-cache-injected stand-in
    declaring FIXED, hand-picked budgets) — the real ``CompactionEngine``
    now computes its OWN budgets (``t_max=7_000`` below forces a small,
    real ``effective_trigger``, mirroring how ``t_max`` is already used
    elsewhere in this file), and the history size below is DERIVED from
    those real, read ``session._compaction_controller._engine.budgets``
    values (never re-hardcoded) — the relationship this test actually
    needs (turns exceed ``head_budget + tail_budget`` so real compaction
    candidates exist, while staying under ``effective_trigger`` so
    ``decompose_history_for_retry`` keeps ``raw_middle`` EMPTY on the
    FIRST outer attempt) — not the exact numbers a stand-in used to
    declare.

    #5612 round-2 (architect ruling, PR review): ``raw_middle`` no longer
    stays empty ACROSS every outer attempt the way the paragraph above
    still correctly describes for the first one. retry_loop's own
    ladder (Phase 1/2) can pull content INTO an initially-empty
    ``raw_middle`` mid-episode and fold it there — that fold is now
    persisted (#5612), so on a LATER outer attempt ``decompose_history_
    for_retry``'s own ``total`` (now: the durable summary's own wire
    size + whatever remains) can legitimately exceed
    ``effective_trigger`` where the pre-fold raw content did not,
    populating ``raw_middle`` again. Verified NOT a bug directly (real
    event trace, this file's own instrumented run): each of these
    intermediate folds genuinely DOES shrink the population it replaces
    (12 raw turns collapse to one compact summary, repeatedly measured),
    so a durable summary re-persisting mid-episode is expected, correct
    behaviour, not a defect this test needs to guard against. (A drafted
    "discard a fold that does not shrink" rule was considered as a
    SEPARATE mechanism during #5612's own review — it would have been
    irrelevant here even had it landed, since these folds do shrink — and
    was independently WITHDRAWN outright, its own premise proven false;
    it is not part of this repo and this paragraph does not rely on it.)
    This test's own recovery now genuinely happens via retry_loop's own
    internal folding at least as often as via ``force_compact_now``'s
    except-block side effect this test's own name still refers to —
    ``_compacted()`` below now recognizes either as "compaction
    happened", matching the SAME underlying fact (a real fold occurred,
    the watermark advanced) two different call sites can now report
    through two different events."""
    session = _make_spill_session(
        tmp_path, monkeypatch, max_shrink_iterations=1, t_max=7_000,
        # recovery_policy="next_turn" (default)
    )
    budgets = session._compaction_controller._engine.budgets
    turn_text = "X" * 320
    turn_tokens = len(turn_text) // 4  # matches CompactionConfig(use_chars4_estimate=True)
    # Comfortably clear head+tail (real candidates exist) while staying
    # under effective_trigger (raw_middle stays empty) — a margin, not an
    # exact boundary; the assert below is this test's own sanity check
    # that the relationship still holds against whatever the real engine
    # computed, not a re-hardcoded number.
    turn_count = (budgets.head_budget + budgets.tail_budget) // turn_tokens + 10
    total_tokens = turn_count * turn_tokens
    assert total_tokens < budgets.effective_trigger, (
        f"test setup sanity: {turn_count} turns ({total_tokens} tokens) must "
        f"stay under effective_trigger={budgets.effective_trigger} — adjust "
        f"t_max or turn_text if this ever fires"
    )
    for _i in range(turn_count):
        # #5514 §7-1: the predicate no longer gates on role=="tool", so a
        # plain "user" turn is spill-eligible by default (LAST_RESORT) —
        # this test's own contract is "nothing spillable", which now
        # requires an explicit NEVER, not just the absence of a tool turn.
        _push(session, "user", turn_text, spillability=Spillability.NEVER)

    # #5467: was a custom ``_on_event``/``compacted`` dict subscriber (a
    # side-effecting callback, not a plain append) reaching
    # ``session._audit_events`` directly — collect_events(session) gives the
    # same raw event list; the "has compaction_completed fired yet" check
    # that used to live in the subscriber is now a plain derivation over
    # that list, computed wherever it's needed instead of tracked as a
    # side-effect.
    events = collect_events(session)

    def _compacted() -> bool:
        # #5612 round-2: a real fold that advanced the watermark now
        # reaches this via EITHER `compaction_completed`
        # (`force_compact_now`'s own `_run_compaction`, this test's own
        # original target mechanism) OR `recovery_summary_persisted`
        # with `outcome="persisted"` (retry_loop's own internal fold,
        # #5612 — a DIFFERENT call site reporting the SAME underlying
        # fact: a fold happened, durably). See this test's own docstring
        # for why both are now live paths to the same recovery.
        return any(
            e.type == "compaction_completed"
            or (e.type == "recovery_summary_persisted" and e.data.get("outcome") == "persisted")
            for e in events
        )

    loop = _ContentDrivenLoop(lambda history, user_text: not _compacted())

    result = asyncio.run(
        session._loop_driver._run_with_shrink_and_byte_reduction(
            loop, "continue please", chain_id="c1",
        )
    )
    assert result is None  # the fake loop's own successful return

    assert _compacted(), (
        "expected a real compaction_completed OR "
        "recovery_summary_persisted(outcome=persisted) event"
    )
    spill_events = [e for e in events if e.type == "tool_result_offloaded"]
    assert not spill_events, (
        "nothing was spillable (no tool-result turns) — spill must not "
        f"have offloaded anything: {[e.data for e in spill_events]!r}"
    )


@pytest.mark.llm_stub
def test_history_dominant_overflow_fails_fast_with_nothing_spillable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: contract acceptance ①b — the TRUE-exhaustion sibling of
    the test above: nothing spillable (no tool-result turns) AND
    compaction ALSO cannot progress (too few durable turns for
    ``force_compact_now``'s own candidate scan to find anything — the
    real engine's ``compact()`` never even gets called; ``@llm_stub`` is
    a defensive safety net, not load-bearing for this scenario).
    Together the two tests distinguish "one axis dry" (recovers) from
    "both axes dry" (true exhaustion, #5364 §1.6's unqualified failure
    predicate: raise immediately, no generous extra attempts)."""
    session = _make_spill_session(
        tmp_path, monkeypatch, max_shrink_iterations=1,
        # recovery_policy="next_turn" (default)
    )
    # #5514 §7-1: NEVER, not merely non-"tool" — same reasoning as the
    # sibling test above.
    _push(session, "user", "hi", spillability=Spillability.NEVER)
    _push(session, "assistant", "ok", spillability=Spillability.NEVER)

    events = collect_events(session)

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


@pytest.mark.llm_stub
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
    forever) rather than silently compacting anyway. ``@llm_stub`` is a
    defensive safety net here too (``recovery_policy="never"`` disables
    compaction entirely regardless of candidate count — a real
    ``compact()`` call, if this ever regressed, should fail loudly rather
    than silently hitting network)."""
    # #5531 §10: max_shrink_iterations no longer bounds retry_loop at all
    # (the parameter is orphaned, see router_loop_driver.py's own call
    # site comment) — kept here only because _make_spill_session still
    # accepts it; termination is now genuinely bounded by the T_max-
    # halving floor instead of this knob.
    session = _make_spill_session(
        tmp_path, monkeypatch, max_shrink_iterations=1, recovery_policy="never",
    )
    for _i in range(50):
        # #5514 §7-1: NEVER — this test's own "zero spillable candidates"
        # sanity now needs an explicit declaration, not just role != "tool".
        _push(session, "user", "X" * 320, spillability=Spillability.NEVER)

    events = collect_events(session)

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
    # candidate exhaustion, not a fixed constant; #5531 §10 additionally
    # removed retry_loop's own max_iterations, so "bounded" is no longer
    # witnessable as a specific call count pinned to a config knob (that
    # would now be a Tier-4 implementation-detail pin, not a structural
    # fact) — the `with pytest.raises(UnrecoveredError)` context manager
    # above IS the bounded-termination witness: this test function
    # returning at all (not timing out under CI's own kill switch) proves
    # the loop genuinely stopped rather than hanging.


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
    claim).

    #5622 (PR review, real-execution finding): turn 1's own recovery
    must go through retry_loop's own internal compact() call at least
    once (a real 413 enters `_run_with_shrink`'s ladder, which invokes
    retry_loop regardless of `recovery_policy`) — with no stub, that
    call reaches real, unpinned litellm and fails the whole turn (the
    pre-#5622 classification bug used to silently absorb that failure
    as a fake OVERFLOW instead of surfacing it). `LLMStub(raise_for=...,
    cause="byte_limit")`, scoped to turn 1's own drive, forces a real,
    byte-limited compact() failure instead — matching #5615's own
    established pattern — so spill (not an opportunistic compact()
    success) is genuinely what recovers turn 1, unchanged from what
    this test already asserts."""
    from reyn.dev.testing.llm_stub import LLMStub

    session = _make_spill_session(tmp_path, monkeypatch)
    _push(session, "user", "look something up")
    huge = "Q" * 50_000
    _push(session, "tool", huge, tool_call_id="tc1", name="tool")
    _push(session, "assistant", "ok, done")

    loop1 = _ContentDrivenLoop(lambda history, user_text: _has_content(history, huge))

    async def _turn1() -> None:
        stub = LLMStub(raise_for=lambda messages: True, cause="byte_limit")
        stub.install()
        try:
            await session._loop_driver._run_with_shrink_and_byte_reduction(
                loop1, "continue please", chain_id="c1",
            )
        finally:
            stub.restore()
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

    #5370/#5390 (#5368 remainder): a THIRD stage — a ``tail`` tool
    candidate, sized larger than mid — closes the SECOND boundary (mid
    before tail). Without it, a regression to ``head -> tail -> mid``
    would still pass: the original form of this test only pinned "head
    is spilled first," never "mid is spilled before tail."

    #5364 §1.6 changed ``_attempt_reactive_spill`` to spill exactly ONE
    candidate per call (return immediately on the first genuine
    progress — #5390's original single-call, 3-events-in-one-pass form
    assumed the OLD "keep going until a byte decrease" behavior, which
    §1.6 replaced). This version calls it 3 times in a row instead: each
    call re-decomposes history fresh, and ``RouterHistoryBuffer.
    is_already_spilled`` (checked by VALUE against the overlay) makes an
    already-spilled candidate's current content skip straight to the
    next one — so 3 sequential calls naturally walk head -> mid -> tail
    without the test driving that skip itself.

    Witnessed via the PUBLIC ``tool_result_offloaded`` audit-event
    (``tool_result_cap.py``'s own emit, threaded through
    ``spill_turn_content``) — its ``total_chars`` names the size of the
    candidate that was JUST spilled, a public order-witness for
    ``RouterLoopDriver._spill_candidates`` without calling that private
    static method directly (CLAUDE.md testing policy: "if neither [a
    public surface nor a snapshot-style read] exists, that absence is the
    finding" — here a public read DOES exist once driven through a real
    spill pass, so this is that seam, not a documented absence).
    ``settle(session)`` is required before reading the
    subscriber's list — events queue, they are not delivered
    synchronously inside ``emit()`` (measured directly: the subscriber
    list was empty without it, on every run)."""
    session = _make_spill_session(tmp_path, monkeypatch, t_max=2_500)
    events = collect_events(session)

    # Head: one small tool turn. Mid: one turn whose content is FAR
    # larger than the head candidate — if global size-sort fired instead
    # of staged order, THIS would be spilled first. Tail: one turn LARGER
    # than mid (#5370) — a size-explained order would spill this before
    # mid, so its size alone can't account for coming after. Padding
    # filler turns (not tool-role, never candidates) so t_max forces a
    # genuine head/mid/tail split with each tool turn landing in its own
    # group.
    small_head_content = "tiny result h1 " + "a" * 10
    huge_mid_content = "M" * 5_000
    huge_tail_content = "T" * 6_000
    # #5514 §7-1: filler turns are plain "user"/"assistant" content, now
    # spill-eligible by default (LAST_RESORT) since the predicate no
    # longer gates on role=="tool" — NEVER keeps them out of contention so
    # this test still isolates the 3 tool candidates' own staged order.
    _push(session, "tool", small_head_content, tool_call_id="tc-h1", name="tool")
    for i in range(20):
        _push(session, "user", f"filler question number {i + 100} " * 8, spillability=Spillability.NEVER)
        _push(session, "assistant", f"filler answer number {i + 100} " * 8, spillability=Spillability.NEVER)
    _push(session, "tool", huge_mid_content, tool_call_id="tc-m1", name="tool")
    for i in range(3):
        _push(session, "user", f"filler question number {i + 300} " * 8, spillability=Spillability.NEVER)
        _push(session, "assistant", f"filler answer number {i + 300} " * 8, spillability=Spillability.NEVER)
    _push(session, "tool", huge_tail_content, tool_call_id="tc-t1", name="tool")

    head, raw_middle, tail, _summary, _seq_by_id = (
        session._loop_driver._history_buffer.decompose_history_for_retry()
    )
    head_ids = {t.get("tool_call_id") for t in head if t.get("role") == "tool"}
    mid_ids = {t.get("tool_call_id") for t in raw_middle if t.get("role") == "tool"}
    tail_ids = {t.get("tool_call_id") for t in tail if t.get("role") == "tool"}
    assert head_ids == {"tc-h1"}, (
        f"test setup sanity: the head candidate must land in head, got {head_ids!r} "
        f"— adjust t_max/turn counts"
    )
    assert mid_ids == {"tc-m1"}, (
        f"test setup sanity: the mid candidate must land in raw_middle, got {mid_ids!r}"
    )
    assert tail_ids == {"tc-t1"}, (
        f"test setup sanity: the tail candidate must land in tail, got {tail_ids!r} "
        f"— adjust t_max/turn counts"
    )

    progressed_1 = await session._loop_driver._attempt_reactive_spill(chain_id="c1")
    await settle(session)
    progressed_2 = await session._loop_driver._attempt_reactive_spill(chain_id="c1")
    await settle(session)
    progressed_3 = await session._loop_driver._attempt_reactive_spill(chain_id="c1")
    await settle(session)
    assert (progressed_1, progressed_2, progressed_3) == (True, True, True), (
        "each of the 3 calls must make genuine progress (one candidate "
        f"each) — got {(progressed_1, progressed_2, progressed_3)!r}"
    )

    offloaded = [e for e in events if e.type == "tool_result_offloaded"]
    # Canonical unpack idiom (testing.ja.md Tier 4 — no bare len()==N/>=N
    # format pin): exactly 3 calls, exactly 3 events — one per call.
    head_event, mid_event, tail_event = offloaded
    first_spilled_size = head_event.data["total_chars"]
    assert first_spilled_size == len(small_head_content), (
        f"the FIRST candidate spilled must be the head turn "
        f"({len(small_head_content)} chars), not the far-larger mid turn "
        f"({len(huge_mid_content)} chars) — got first_spilled_size="
        f"{first_spilled_size}. A global size-sort would spill the mid "
        f"candidate first."
    )
    second_spilled_size = mid_event.data["total_chars"]
    assert second_spilled_size == len(huge_mid_content), (
        f"the SECOND call must spill the mid turn "
        f"({len(huge_mid_content)} chars), not the LARGER tail turn "
        f"({len(huge_tail_content)} chars) — got second_spilled_size="
        f"{second_spilled_size}. A global size-sort would spill the tail "
        f"candidate (the largest) before mid; a head->tail->mid regression "
        f"would also put tail here."
    )
    third_spilled_size = tail_event.data["total_chars"]
    assert third_spilled_size == len(huge_tail_content), (
        f"the THIRD call must spill the tail turn "
        f"({len(huge_tail_content)} chars) — got third_spilled_size="
        f"{third_spilled_size}."
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

    #5382: the real ``CompactionEngine`` now runs throughout (was a
    hand-rolled ``_SpillableByteLimitMidEngine`` stand-in whose own tiny
    injected ``ComputedBudgets`` forced the head/tail split — that
    private-cache injection is exactly what #5382 closes).

    #5474 lead-coder BLOCKING: the FIRST revision re-hardcoded
    ``t_max=2_500`` + hand-picked filler counts, assuming (untested) that
    this reproduced the same head/raw_middle/tail split the retired
    stand-in's FIXED ``budgets(20/20/3000)`` used to force — "replacing a
    baked number with another baked number is not a migration" (lead-coder,
    verbatim). It did not: the real engine derives its own
    head_budget/tail_budget from ``t_max``, and the turn sizes below now
    derive from THOSE real, read values instead of re-guessing a shape
    that happened to work once.

    The relationship this test needs — head turn alone in ``head``, the
    marker turn alone in ``raw_middle``, every filler turn inside
    ``tail`` — is built directly from ``trim_head``/``trim_tail``'s own
    group-accumulation rule (``_trim_groups``, engine.py): the FIRST group
    a trim scan sees is always kept whole even when it alone exceeds the
    budget (Axis 7 keep-whole), and every later group is kept only while
    the running total stays ``<= budget``.

    - **Head**: the head turn's own token count is deliberately built to
      exceed ``head_budget`` on its own (``effective_trigger + tail_budget
      + a margin`` — always ``> head_budget``, since ``head_budget <=
      main_M_room <= effective_trigger`` by ``compute_budgets``'s own
      formula). ``trim_head`` then keeps this ONE turn via the
      over-budget keep-whole branch and stops — the marker is never even
      examined, independent of the exact head/tail split a given
      ``t_max`` produces. The same oversize also guarantees the WHOLE
      history's token total clears ``effective_trigger`` (the elide
      branch this test needs at all), without touching the tail-side
      arithmetic below.
    - **Tail**: ``_FILLER_COUNT`` uniform filler turns are sized so their
      summed tokens are ``tail_budget - r`` for some ``0 <= r <
      _FILLER_COUNT`` (an integer-division remainder, never re-guessed) —
      comfortably kept whole by ``trim_tail`` (total ``<= tail_budget``),
      while the marker turn's own (fixed, ~5-token) size is built larger
      than that remainder can ever be, so adding the marker to the
      running total always overshoots ``tail_budget`` and ``trim_tail``
      excludes it right there. This holds for ANY real ``tail_budget`` —
      nothing here is re-fit to one ``t_max`` value.

    ``LLMStub``'s ``raise_for=<callable(messages) -> bool>`` (architect
    ruling, #5382's "raise_for generalization") makes ONLY the compact()
    call carrying the marker's content raise — a CONTENT predicate, never
    a call count (architect: a counter that increments at compact()'s own
    entry cannot say whether the right content arrived — the exact weak
    witness #5386 corrected). ``max_shrink_iterations=25`` is generous:
    the halving ladder needs a few attempts to reach the mid=1 floor,
    then (after the spill succeeds) retry_loop folds the remaining filler
    turns one at a time before ever reaching ``main_call`` — a real, if
    wasteful, consequence of #4947 ③'s "don't reset the discovered slice
    size to full" choice, not a bug this test is pinning."""
    from reyn.dev.testing.llm_stub import LLMStub

    session = _make_spill_session(
        tmp_path, monkeypatch, t_max=2_500, max_shrink_iterations=25,
        recovery_policy="never",
    )
    budgets = session._compaction_controller._engine.budgets

    marker_text = "OVERSIZED_MARKER_5367_3"
    marker_tokens = max(1, len(marker_text) // 4)

    # Head: deliberately larger than head_budget (and than
    # effective_trigger + tail_budget) so `trim_head`'s FIRST group is
    # kept whole via the over-budget branch and the scan stops there —
    # the marker is structurally never reached, and the whole-history
    # total structurally clears effective_trigger. See the docstring
    # above for why this holds independent of the real t_max/budgets.
    head_tokens = budgets.effective_trigger + budgets.tail_budget + 1_000
    head_text = "H" * (head_tokens * 4)

    # Tail: _FILLER_COUNT uniform turns summing to tail_budget - r
    # (r = tail_budget % _FILLER_COUNT < _FILLER_COUNT <= marker_tokens),
    # so trim_tail keeps every filler turn whole and then excludes the
    # marker (filler_total + marker_tokens always overshoots tail_budget
    # by construction). See the docstring above.
    _FILLER_COUNT = 4
    assert _FILLER_COUNT <= marker_tokens, (
        "test setup sanity: _FILLER_COUNT must not exceed marker_tokens — "
        "the remainder r = tail_budget % _FILLER_COUNT must stay strictly "
        "below marker_tokens for the tail-exclusion arithmetic above to "
        "hold for ANY real tail_budget"
    )
    per_filler_tokens = max(1, budgets.tail_budget // _FILLER_COUNT)
    filler_text = "F" * (per_filler_tokens * 4)

    # #5382: content-based witness (architect ruling) — records whether
    # EACH compact() call that reached this predicate carried the marker,
    # rather than counting how many calls happened. The predicate's own
    # decision (raise or not) is exactly "did this call carry the
    # marker", so recording that decision IS the content record — no
    # separate tracking mechanism, no call count.
    seen_marker_per_call: "list[bool]" = []

    def _raise_on_marker_content(messages: list) -> bool:
        has_marker = "OVERSIZED_MARKER_5367_3" in messages[-1].get("content", "")
        seen_marker_per_call.append(has_marker)
        return has_marker

    stub = LLMStub(raise_for=_raise_on_marker_content, cause="byte_limit")
    stub.install()
    try:
        _push(session, "user", head_text)
        _push(session, "tool", marker_text, tool_call_id="tc-marker", name="big_tool")
        for _i in range(_FILLER_COUNT):
            _push(session, "user", filler_text)

        head, raw_middle, _tail, _summary, _seq_by_id = (
            session._loop_driver._history_buffer.decompose_history_for_retry()
        )
        mid_ids = {t.get("tool_call_id") for t in raw_middle if t.get("role") == "tool"}
        assert mid_ids == {"tc-marker"}, (
            f"test setup sanity: the marker turn must land alone in "
            f"raw_middle's tool turns, got {mid_ids!r} — head_tokens="
            f"{head_tokens}, per_filler_tokens={per_filler_tokens}, "
            f"budgets={budgets!r} (derived from real budgets, not a "
            f"re-guessed t_max — see this test's own docstring for the "
            f"construction)"
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
    finally:
        stub.restore()

    # #5382: content-based witness, replacing the old `compact_calls >= 2`
    # counter. `seen_marker_per_call` is a record of what content EACH
    # compact() call actually carried, filled by the predicate itself —
    # `True in ...` proves the marker's own content genuinely reached
    # compact() and was made to fail; `False in ...` proves a LATER call,
    # with DIFFERENT (spilled) content, also reached compact() and
    # succeeded — the spilled content actually got there, not merely "2
    # calls happened" (a counter alone cannot distinguish "spilled content
    # arrived" from "the SAME marker content was retried twice").
    assert True in seen_marker_per_call, (
        f"test setup sanity: expected at least one compact() call to "
        f"carry the marker content — got {seen_marker_per_call!r}"
    )
    assert False in seen_marker_per_call, (
        f"expected a LATER compact() call to carry DIFFERENT (spilled) "
        f"content — got {seen_marker_per_call!r}, meaning the spilled "
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
    events = collect_events(session)

    _push(session, "tool", "huge tool result " + "z" * 5_000, tool_call_id="tc-1", name="tool")
    for i in range(20):
        _push(session, "user", f"filler question number {i} " * 8)
        _push(session, "assistant", f"filler answer number {i} " * 8)

    await session._loop_driver._attempt_reactive_spill(chain_id="c1")
    await settle(session)

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
    # #5514 §7-1: NEVER — see the staged-order test's own comment above.
    for i in range(20):
        _push(session, "user", f"filler question number {i + 100} " * 8, spillability=Spillability.NEVER)
        _push(session, "assistant", f"filler answer number {i + 100} " * 8, spillability=Spillability.NEVER)
    _push(session, "tool", "result body " * 100, tool_call_id="tc-mid", name="tool")
    for i in range(3):
        _push(session, "user", f"filler question number {i + 300} " * 8, spillability=Spillability.NEVER)
        _push(session, "assistant", f"filler answer number {i + 300} " * 8, spillability=Spillability.NEVER)

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

    #5531 §10: the second scenario below (``session2``) drives
    ``_run_with_shrink_and_byte_reduction``, which now genuinely reaches
    ``engine.compact()`` (§10's own rung① spill-first reordering means
    retry_loop's internal ladder, not just this test's own direct
    ``_attempt_reactive_spill`` calls, participates) — an unstubbed real
    completion call is exactly the gap ``_make_spill_session``'s own
    docstring already names ("a test whose own scenario reaches an
    actual compact() call must mark itself llm_stub").

    #5612 round-2 (architect ruling, PR review): a BARE
    ``@pytest.mark.llm_stub`` (unconditional success, no real limit) used
    to make retry_loop's own FIRST ``compact()`` attempt trivially fold
    ALL of ``raw_middle`` — including the 3 mid candidates — in one shot,
    since rung① (``_spill_batch_from_offered``, engine.py) only fires
    when ``compact()`` itself OVERFLOWS, which a limitless stub never
    does. Pre-#5612 this was harmless: that fold was transport-only and
    discarded, so the NEXT outer ``_run_with_shrink`` attempt's own
    ``decompose_history_for_retry()`` call re-derived a fresh,
    un-folded view and the driver-level ``_attempt_reactive_spill``
    (called only AFTER an ``UnrecoveredError``, router_loop_driver.py)
    could still find and spill the mid candidates one at a time — this
    test was GREEN, but only because the fold it depended on being
    UNDONE was a bug #5612 fixes (owner ruling: a durable fold must
    never revert on the next turn). Once #5612 makes that fold durable,
    the SAME candidates are durably covered — invisible to EVERY
    later decompose call, including ``_attempt_reactive_spill``'s own —
    the exact "history never grows back" invariant #5612 exists to
    establish. ``session2`` below now uses ``LLMStub(raise_for=...,
    cause="byte_limit")`` scoped to its own drive (``session``'s own
    direct ``_attempt_reactive_spill`` calls above never reach
    ``compact()`` at all, so the bare stub stays correct there) so
    ``compact()`` genuinely OVERFLOWS while any of the 3 mid
    candidates' own un-replaced content is still present — forcing
    rung① to fire through its own real, production-shaped path, exactly
    as a real, size-limited provider would, instead of a stub that lets
    compact() silently absorb everything no real provider could accept
    in one call.
    """
    from reyn.dev.testing.llm_stub import LLMStub

    session = _make_spill_session(tmp_path, monkeypatch, t_max=2_500)
    # #5514 §7-1: NEVER — see the staged-order test's own comment above;
    # this test's own "mid-only" isolation depends on the filler turns
    # never being offered.
    for i in range(3):
        for j in range(8):
            _push(session, "user", f"filler {i}-{j} " * 8, spillability=Spillability.NEVER)
            _push(session, "assistant", f"filler reply {i}-{j} " * 8, spillability=Spillability.NEVER)
        _push(session, "tool", f"mid result {i} " * 100, tool_call_id=f"tc-mid{i}", name="tool")
    for i in range(3):
        _push(session, "user", f"tail filler {i} " * 8, spillability=Spillability.NEVER)
        _push(session, "assistant", f"tail reply {i} " * 8, spillability=Spillability.NEVER)

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
    # #5514 §7-1: NEVER — see the staged-order test's own comment above;
    # this test's own "exactly 3" isolation depends on the filler turns
    # never being offered.
    for i in range(3):
        for j in range(8):
            _push(session2, "user", f"filler {i}-{j} " * 8, spillability=Spillability.NEVER)
            _push(session2, "assistant", f"filler reply {i}-{j} " * 8, spillability=Spillability.NEVER)
        _push(session2, "tool", f"mid result {i} " * 100, tool_call_id=f"tc-mid{i}", name="tool")
    for i in range(3):
        _push(session2, "user", f"tail filler {i} " * 8, spillability=Spillability.NEVER)
        _push(session2, "assistant", f"tail reply {i} " * 8, spillability=Spillability.NEVER)
    # #5467: was a custom counting subscriber (a side-effecting callback,
    # not a plain append) reaching ``session2._audit_events`` directly —
    # collect_events(session2) gives the same raw event list; the running
    # spill count that used to live in the subscriber is now a plain
    # derivation over that list.
    events2 = collect_events(session2)

    def _spilled_so_far() -> int:
        return sum(1 for e in events2 if e.type == "tool_result_offloaded")

    # #5612 round-2: force compact() to genuinely OVERFLOW (a real
    # provider's own reaction to this much content) while ANY of the 3
    # mid candidates' own un-replaced "mid result N " text is still
    # present — content-based (#5382 architect ruling), never a call
    # count. This is what makes rung① (retry_loop's own internal spill,
    # engine.py) fire through its real, production-shaped path instead
    # of retry_loop's first compact() call silently absorbing all 3
    # candidates in one always-succeeding stub call — see this test's
    # own docstring above for the full trace.
    _mid_markers = tuple(f"mid result {i} " for i in range(3))

    def _raise_while_any_mid_candidate_unspilled(messages: list) -> bool:
        content = messages[-1].get("content", "") if messages else ""
        return any(marker in content for marker in _mid_markers)

    stub2 = LLMStub(
        raise_for=_raise_while_any_mid_candidate_unspilled, cause="byte_limit",
    )
    stub2.install()
    try:
        loop2 = _ContentDrivenLoop(lambda history, user_text: _spilled_so_far() < 3)
        result = asyncio.run(
            session2._loop_driver._run_with_shrink_and_byte_reduction(
                loop2, "continue please", chain_id="c1",
            )
        )
    finally:
        stub2.restore()
    assert result is None, "the turn must ultimately succeed once all mid candidates spilled"
    assert _spilled_so_far() == 3, (
        "the turn must have spilled all 3 mid candidates before succeeding "
        f"— got {_spilled_so_far()!r}"
    )


# ── #5592 REQUIRED acceptance: whole-tier batching, head/tail never merged ──


def test_tier_granularity_spills_whole_mid_tier_in_one_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5592 accept — with ``spill_granularity="tier"`` (the new
    default), a SINGLE ``_attempt_reactive_spill`` call spills ALL
    candidates sharing the same face + tier, not one — contrasting
    directly with ``test_mid_only_spill_bounds_by_candidate_exhaustion_
    wire_bytes_never_move`` above, which pins the OLD ``"turn"``
    (one-at-a-time) behavior for the SAME 3-mid-candidate shape and needs
    3 rounds. Here, round 1 alone must consume all 3.

    Strip-falsify: setting ``spill_granularity="turn"`` on this exact
    setup reproduces the OLD 3-rounds-for-3-candidates shape (proven by
    the sibling ``turn``-granularity test above, same fixture) — the
    only variable between the two is this one config field."""
    session = _make_spill_session(
        tmp_path, monkeypatch, t_max=2_500, spill_granularity="tier",
    )
    for i in range(3):
        for j in range(8):
            _push(session, "user", f"filler {i}-{j} " * 8, spillability=Spillability.NEVER)
            _push(session, "assistant", f"filler reply {i}-{j} " * 8, spillability=Spillability.NEVER)
        _push(session, "tool", f"mid result {i} " * 100, tool_call_id=f"tc-mid{i}", name="tool")
    for i in range(3):
        _push(session, "user", f"tail filler {i} " * 8, spillability=Spillability.NEVER)
        _push(session, "assistant", f"tail reply {i} " * 8, spillability=Spillability.NEVER)

    head, raw_middle, tail, _summary, _ = (
        session._loop_driver._history_buffer.decompose_history_for_retry()
    )
    mid_ids = {t.get("tool_call_id") for t in raw_middle if t.get("role") == "tool"}
    assert mid_ids == {"tc-mid0", "tc-mid1", "tc-mid2"}, (
        f"test setup sanity: all 3 candidates must land in raw_middle "
        f"alone, got {mid_ids!r} — adjust t_max/filler counts"
    )

    progressed = asyncio.run(session._loop_driver._attempt_reactive_spill(chain_id="c1"))
    assert progressed is True

    _, raw_middle_after, _, _, _ = (
        session._loop_driver._history_buffer.decompose_history_for_retry()
    )
    still_original = {
        t.get("tool_call_id") for t in raw_middle_after
        if t.get("role") == "tool" and "read_file(path=" not in t.get("content", "")
    }
    assert still_original == set(), (
        f"expected ALL 3 mid candidates spilled in the ONE call (tier "
        f"granularity) — {still_original!r} still unspilled"
    )


def test_head_and_tail_batches_never_merge_into_one_spill_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5592 deny (owner ruling, "head と tail をまとめない") — with
    ONE candidate in head and ONE in tail, a single
    ``_attempt_reactive_spill`` call (``spill_granularity="tier"``) must
    spill ONLY the head candidate (staged priority: head before tail,
    unchanged since #5364) while leaving the tail candidate entirely
    untouched — if a batch ever crossed the head/tail boundary, BOTH
    would spill in this one call instead.

    Real ``Session``/``RouterLoopDriver``/``RouterHistoryBuffer``/
    ``MediaStore`` throughout (same idiom every other test in this file
    uses) — no fake collaborator standing in for a cheaply-constructible
    real one."""
    session = _make_spill_session(
        tmp_path, monkeypatch, t_max=2_500, spill_granularity="tier",
    )
    _push(session, "tool", "tiny head result " + "a" * 10, tool_call_id="tc-head-a", name="tool")
    for i in range(20):
        _push(session, "user", f"mid filler {i} " * 8, spillability=Spillability.NEVER)
        _push(session, "assistant", f"mid reply {i} " * 8, spillability=Spillability.NEVER)
    _push(session, "tool", "tail result " * 60, tool_call_id="tc-tail-a", name="tool")

    head, raw_middle, tail, _summary, _ = (
        session._loop_driver._history_buffer.decompose_history_for_retry()
    )
    head_tool_ids = {t.get("tool_call_id") for t in head if t.get("role") == "tool"}
    tail_tool_ids = {t.get("tool_call_id") for t in tail if t.get("role") == "tool"}
    assert head_tool_ids == {"tc-head-a"}, (
        f"test setup sanity: the head candidate must land in head alone "
        f"— got {head_tool_ids!r}"
    )
    assert tail_tool_ids == {"tc-tail-a"}, (
        f"test setup sanity: the tail candidate must land in tail alone "
        f"— got {tail_tool_ids!r}"
    )

    progressed = asyncio.run(session._loop_driver._attempt_reactive_spill(chain_id="c1"))
    assert progressed is True

    head_n, _raw_middle_n, tail_n, _summary_n, _ = (
        session._loop_driver._history_buffer.decompose_history_for_retry()
    )
    head_still_original = {
        t.get("tool_call_id") for t in head_n
        if t.get("role") == "tool" and "read_file(path=" not in t.get("content", "")
    }
    tail_still_original = {
        t.get("tool_call_id") for t in tail_n
        if t.get("role") == "tool" and "read_file(path=" not in t.get("content", "")
    }
    assert head_still_original == set(), (
        f"expected the head candidate spilled in the ONE call — "
        f"{head_still_original!r} still unspilled"
    )
    assert tail_still_original == {"tc-tail-a"}, (
        f"the tail candidate must be UNTOUCHED — the batch that consumed "
        f"head must never reach across to tail — got "
        f"{tail_still_original!r}"
    )


# ── #5531 §9.6 REQUIRED acceptance: population count + tier breakdown ──────


@pytest.mark.llm_stub
def test_spill_population_exhausted_reports_count_and_breakdown_when_all_never(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5531 §9.6 — "candidate 0" is ambiguous by itself: a
    genuinely all-``NEVER`` population (nothing left to spill, correct)
    and a broken population-construction path (a real bug) produce the
    SAME zero-candidate observation with no other signal. This test
    drives the REAL ``_spill_fn`` closure (``RouterLoopDriver``, via a
    genuine ``compact()``-origin overflow — never called directly) into
    exactly that zero-candidate case with a population that is
    deliberately, entirely ``NEVER`` (never a missing-key accident), and
    asserts the emitted ``spill_candidate_population_exhausted`` event's
    counts add up to the real population size — the accept-side witness
    that the reporting mechanism itself works, not just that it exists.

    Strip-falsify (performed during review): removing the
    ``self._events.emit("spill_candidate_population_exhausted", ...)``
    call at ``_spill_fn``'s own exhaustion return site makes
    ``population_events`` empty and the ``assert population_events``
    below fail — the event genuinely does not fire without that line.
    """
    from reyn.dev.testing.llm_stub import LLMStub

    session = _make_spill_session(tmp_path, monkeypatch, t_max=2_500)

    def _always_fail_compaction(messages: list) -> bool:
        return True

    stub = LLMStub(raise_for=_always_fail_compaction, cause="byte_limit")
    stub.install()
    try:
        # #5514 §7-1: every candidate that could land in raw_middle is
        # explicitly NEVER — a deliberate, complete population, not an
        # accidental gap (the exact distinction this test's own event
        # exists to make legible). Filler (also NEVER, mirroring the
        # staged-order test's own isolation pattern above) surrounds the
        # 2 real candidates so t_max=2_500 genuinely splits head/mid/tail
        # instead of fitting everything in head alone.
        for i in range(20):
            _push(session, "user", f"filler question number {i} " * 8, spillability=Spillability.NEVER)
            _push(session, "assistant", f"filler answer number {i} " * 8, spillability=Spillability.NEVER)
        for i in range(2):
            _push(
                session, "tool", f"unspillable result {i} " * 50,
                tool_call_id=f"tc-never{i}", name="tool",
                spillability=Spillability.NEVER,
            )
        for i in range(3):
            _push(session, "user", f"tail filler {i} " * 8, spillability=Spillability.NEVER)
            _push(session, "assistant", f"tail reply {i} " * 8, spillability=Spillability.NEVER)

        head, raw_middle, _tail, _summary, _ = (
            session._loop_driver._history_buffer.decompose_history_for_retry()
        )
        # #5514 §7-1: spillability doesn't influence decompose's OWN
        # chronological head/mid/tail split — only which of raw_middle's
        # turns spill_fn later treats as eligible — so raw_middle also
        # holds the surrounding NEVER-tagged filler; every one of it is
        # still NEVER, which is exactly this test's own point (a large,
        # entirely-unspillable population, not just the 2 tool turns).
        mid_ids = {t.get("tool_call_id") for t in raw_middle if t.get("role") == "tool"}
        assert {"tc-never0", "tc-never1"} <= mid_ids, (
            f"test setup sanity: both NEVER tool turns must land in "
            f"raw_middle — got {mid_ids!r} — adjust t_max/content size"
        )
        assert all(t.get("spillability") == "never" for t in raw_middle), (
            "test setup sanity: this test's own point is a population that "
            "is ENTIRELY never — some raw_middle turn isn't"
        )
        raw_middle_population = len(raw_middle)

        events = collect_events(session)

        with pytest.raises(UnrecoveredError):
            asyncio.run(
                session._loop_driver._run_with_shrink(
                    _ContentDrivenLoop(lambda history, user_text: True),
                    "continue please", chain_id="c1",
                )
            )
        asyncio.run(settle(session))
    finally:
        stub.restore()

    population_events = [
        e for e in events if e.type == "spill_candidate_population_exhausted"
    ]
    # rung① exhausts on every ladder iteration this all-NEVER population
    # reaches (each re-scans raw_middle fresh, #9.5's own no-cursor rule)
    # — at least one, never pinning the exact ladder-iteration count
    # (six questions Q2).
    assert population_events, (
        "expected at least one spill_candidate_population_exhausted event "
        "— the exhaustion-reporting mechanism never fired"
    )
    for only in population_events:
        # #5592 (owner ruling, correcting #9.6): the population this
        # closure is offered is now the SLICE retry_loop is actually
        # sending this attempt (``raw_middle[:_attempt_len]``), not
        # ``raw_middle`` in its entirety once rung② has halved at least
        # once — so this can be STRICTLY LESS than
        # ``raw_middle_population`` (never pinning the exact
        # ``_attempt_len`` value itself, an algorithm-level detail this
        # test does not own). Every member of it is still NEVER either
        # way (the whole history here is NEVER), so the tier breakdown
        # invariant below still fully accounts for it.
        assert 0 < only.data["population"] <= raw_middle_population
        assert only.data["never_count"] == only.data["population"]
        assert only.data["first_choice_count"] == 0
        assert only.data["last_resort_count"] == 0
        assert only.data["last_resort_count"] == 0
        # The accept-side witness itself: a correctly-built population's
        # tier counts sum to exactly the population — the sum a reader
        # would check to rule out "the construction path silently
        # dropped a candidate".
        assert (
            only.data["first_choice_count"]
            + only.data["last_resort_count"]
            + only.data["never_count"]
        ) == only.data["population"]
