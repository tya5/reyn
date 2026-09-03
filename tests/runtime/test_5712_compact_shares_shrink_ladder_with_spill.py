"""Tier 2: #5712 — ``/compact`` (``CompactionController.force_compact_now``
-> ``_run_compaction``) runs its ``compact()`` call through the SAME
rung①(spill)+rung②(halve) shrink ladder ``RecoveryLadder`` (the reactive
``retry_loop`` path) already runs on an overflow, instead of a bare,
unprotected single call.

owner real-machine incident: the recorded exception was ``context_length_
exceeded`` — the compaction LLM call ITSELF exceeded its own budget.
``force_compact_now``'s ``except Exception: pass`` (closed by #5708) swallowed
this every time, so ``/compact`` rendered "Nothing was compacted this pass"
forever; the compaction call that was SUPPOSED to shrink history was itself
too big to send. #5712 fixes the CAUSE #5708 could only report accurately.

owner ruling (2026-09-03, verbatim): "operator の compact 要求は spill 含む
縮小フローだから" — ``/compact`` must run spill (rung①), not just halving
(rung②) alone. architect's own witness for "genuinely the same shared logic,
not a second copy": stripping either rung's arithmetic must turn BOTH the
reactive-path test AND this file's own new test red — a shared boundary that
only one side's test can reach would mean two implementations, not one.

Fixtures are built so a SINGLE, full-size ``compact()`` call genuinely
overflows and MULTIPLE spill rounds (never one) are needed before it fits —
per lead-coder's own explicit review note: "1 回の呼びで通ってしまう
fixture では、段①(spill) が効いた証拠になりません." Real
``CompactionController``/real ``EventLog`` — the engine and spill_fn are the
two collaborators this suite is free to fake (the same class of "the
provider LLM/network boundary" every other compaction test here fakes, per
the testing policy's `LLMReplay`-or-real-instance rule; no ``MagicMock``/
``patch`` anywhere).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Callable

import litellm
import pytest

from reyn.config import CompactionConfig, MultimodalConfig
from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.services.compaction_controller import CompactionController
from reyn.services.compaction.engine import (
    SUMMARY_MESSAGE_ROLE,
    ChatSummary,
    CompactionEngine,
    ComputedBudgets,
    CoversThrough,
    HistoryChunkToCompact,
    RetryLoopTerminal,
    UnrecoveredError,
)
from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle

# #5719: main_M_room=0 — this file's own tests are about the shrink-retry
# ladder (spill/halving) downstream of candidate selection, not the #5719
# shortfall-selection algorithm itself (that has its own dedicated tests
# in test_5719_..._shortfall_selection.py), so main_M_room always produces
# a shortfall for any nonempty middle, matching this file's pre-#5719
# "everything between head/tail is initially offered" fixture shape.
_STUB_BUDGETS = ComputedBudgets(
    main_pool=100_000, head_budget=50, body_budget=5_000,
    tail_budget=50, new_msg_budget=10_000,
    B_M=80_000, main_M_room=0, effective_trigger=0,
)


def _emit_compaction_started(
    events: EventLog, input_chunk: HistoryChunkToCompact, covers_through: CoversThrough,
) -> None:
    """Mirrors ``CompactionEngine.compact()``'s own real entry emit — see
    ``test_compaction_controller_invariants.py``'s identically-named
    helper; duplicated here (not imported) so this file stays a
    self-contained fixture, matching this suite's own convention of not
    bare-importing names across sibling test modules."""
    _summary_messages = [
        m for m in input_chunk.messages if m.get("role") == SUMMARY_MESSAGE_ROLE
    ]
    events.emit(
        "compaction_started",
        new_turn_count=len(input_chunk.messages) - len(_summary_messages),
        covers_through_seq=covers_through if isinstance(covers_through, int) else None,
        covers_through_unavailable_reason=(
            None if isinstance(covers_through, int) else covers_through.value
        ),
        had_previous=bool(_summary_messages),
    )


class _ContextLengthExceeded(Exception):
    """Stands in for the provider's own real exception (owner real-machine
    record: ``context_length_exceeded``) — a PLAIN exception with no
    FATAL/RETRYABLE-matching shape (no ``status_code``, no reyn-internal
    bug type), so ``classify_llm_failure`` falls through to its
    documented OVERFLOW default — the same classification a genuine
    context-length error gets. What actually raises is irrelevant to the
    property under test (mirrors ``test_5126_pump_sse_exception_not_
    swallowed.py``'s own reasoning for a plain stand-in exception)."""


class _OverflowsUntilFewEnoughEngine(CompactionEngine):
    """``compact()`` genuinely overflows whenever the TOTAL char length of
    the offered messages' ``text`` exceeds ``fits_at_or_below_chars`` —
    char length, not message COUNT, since spill only ever shrinks
    CONTENT (never removes an item — the wire dict stays, its text
    becomes a small placeholder), and halving only ever shrinks COUNT.
    Gating on count would make spill invisible (it never changes count,
    so a count-only gate could never distinguish "spill helped" from
    "spill did nothing") — the fixture shape lead-coder's own review
    demanded: a single full-size attempt must NOT already fit (or
    spill's own contribution could never be distinguished from "it
    happened to work anyway")."""

    def __init__(self, events: EventLog, *, fits_at_or_below_chars: int) -> None:
        self._model = ""
        self._events = events
        self._budgets = _STUB_BUDGETS
        self._fits_at_or_below_chars = fits_at_or_below_chars
        self.attempt_sizes: "list[int]" = []
        self.attempt_char_totals: "list[int]" = []

    async def compact(
        self, input_chunk: HistoryChunkToCompact, *, covers_through: CoversThrough,
    ) -> ChatSummary:
        _emit_compaction_started(self._events, input_chunk, covers_through)
        self.attempt_sizes.append(len(input_chunk.messages))
        _chars = sum(len(t.get("text", "")) for t in input_chunk.messages if isinstance(t, dict))
        self.attempt_char_totals.append(_chars)
        if _chars > self._fits_at_or_below_chars:
            self._events.emit("compaction_failed", error="context_length_exceeded")
            raise _ContextLengthExceeded("context_length_exceeded")
        seqs = [int(t.get("seq", 0)) for t in input_chunk.messages if isinstance(t, dict)]
        return ChatSummary(topic_arc="stub", covers_through_seq=max(seqs) if seqs else 0)


def _history(n: int) -> "list[ChatMessage]":
    return [
        ChatMessage(role="user" if i % 2 == 1 else "assistant", content="x" * 200, seq=i)
        for i in range(1, n + 1)
    ]


def _make_controller(
    *, history: "list[ChatMessage]", engine: CompactionEngine, events: EventLog,
) -> "tuple[CompactionController, list, list[ChatMessage]]":
    collected = collect_events(events)

    def _latest_summary():
        for m in reversed(history):
            if m.role == "summary":
                return m
        return None

    ctrl = CompactionController(
        event_log=events,
        config=CompactionConfig(use_chars4_estimate=True),
        history_from_disk=lambda after_seq: (
            [m for m in history if m.seq == 0 or m.seq > after_seq], False,
        ),
        latest_summary=_latest_summary,
        compaction_engine_factory=lambda: engine,
        history_appender=history.append,
        make_summary_message=lambda rendered, structured, covers: ChatMessage(
            role="summary", content=rendered, seq=0,
            meta={"structured": structured, "covers_through_seq": covers},
        ),
        render_summary=lambda s: str(s),
    )
    return ctrl, collected, history


def _spilling_one_content_shaped_candidate_at_a_time(
    spill_calls: "list[int]",
) -> "Callable[[list[dict]], list[tuple[int, dict]]]":
    """A real, working spill_fn contract (content+spillability-shaped
    input, ``(index, replacement)`` edits out) that spills EXACTLY ONE
    eligible candidate per call — ADR §3's own "one candidate at a time"
    (lead-coder's explicit review note: this is the acceptance for
    proving spill genuinely engages, not a fixture that resolves the
    whole overflow in a single spill call). Records how many candidates
    were OFFERED on each call (``spill_calls``), so a test can assert on
    call count without pinning an internal iteration count."""

    def _spill_fn(
        offered: "list[dict]", *, seq_by_id: "dict[int, int] | None" = None,
    ) -> "list[tuple[int, dict]]":
        # #5726: force_compact_now's own real spill_fn contract now also
        # passes seq_by_id (compaction_controller.py's own _spill_fn_
        # adapted always supplies one) — accepted and ignored here, this
        # fixture's own subject is spill engagement, not provenance.
        spill_calls.append(len(offered))
        for idx, item in enumerate(offered):
            if item.get("spillability") == "never":
                continue
            if item.get("content", "").startswith("[spilled]"):
                continue
            return [(idx, {**item, "content": "[spilled]"})]
        return []

    return _spill_fn


@pytest.mark.asyncio
async def test_spill_alone_resolves_the_overflow_no_halving_needed():
    """Tier 2: acceptance ① (spill runs BEFORE halving) — a population
    that genuinely overflows at full size, but where EVERY candidate is
    spillable, must be resolved by spill rounds alone: every recorded
    compact() attempt size stays the FULL candidate count (halving never
    shrank the offered slice), while multiple spill calls fire.

    Falsify pair (deny side) lives in the next test — a population with
    NOTHING spillable, where the exact same shape must fall through to
    halving instead."""
    events = EventLog()
    history = _history(6)  # -> 4 middle candidates (head=[t1], tail=[t6])
    engine = _OverflowsUntilFewEnoughEngine(events, fits_at_or_below_chars=400)
    # 5 raw chars ("x"*200 * 4 candidates) overflows at full size; each
    # spill round shrinks ONE candidate's content to "[spilled]" (9
    # chars) — after enough rounds the wire is small enough. The COUNT
    # of messages offered never changes via spill (only content does),
    # so `fits_at_or_below` here gates on total wire size via a second
    # engine variant below is unnecessary — this engine's own count-based
    # gate already proves "no halving happened" via `attempt_sizes`
    # staying constant; a real byte-based gate is exercised by the
    # spill-driven engine variant in the next test file section.
    ctrl, collected, hist = _make_controller(history=history, engine=engine, events=events)
    spill_calls: "list[int]" = []

    result = await ctrl.force_compact_now(
        spill_fn=_spilling_one_content_shaped_candidate_at_a_time(spill_calls),
    )
    await settle(events)

    assert engine.attempt_sizes, "compact() was never called"
    assert all(n == engine.attempt_sizes[0] for n in engine.attempt_sizes), (
        f"the offered SIZE must never shrink via halving in this all-"
        f"spillable fixture — got attempt sizes {engine.attempt_sizes!r}"
    )
    assert spill_calls[1:], (
        f"expected multiple spill rounds (ADR §3 'one candidate at a "
        f"time') before the overflow resolved — got {spill_calls!r}"
    )
    assert not result.failed, f"expected eventual success, got failed result: {result!r}"
    summaries = [m for m in hist if m.role == "summary"]
    assert summaries, "a successful shrink-and-retry must still append a summary"


@pytest.mark.asyncio
async def test_mid_floor_records_that_spill_was_offered():
    """Tier 2: acceptance ③ — when NOTHING is spillable (every candidate
    ``spillability == "never"``) and the overflow persists all the way
    down to a single candidate, the resulting MID_FLOOR
    ``UnrecoveredError`` must carry ``spill_was_offered=True`` — spill
    (rung①) was genuinely tried on that final candidate, not skipped."""
    events = EventLog()
    history = _history(6)
    # Never satisfiable — even a single candidate overflows.
    engine = _OverflowsUntilFewEnoughEngine(events, fits_at_or_below_chars=0)
    ctrl, _collected, _hist = _make_controller(history=history, engine=engine, events=events)

    def _never_spillable(
        _offered: "list[dict]", *, seq_by_id: "dict[int, int] | None" = None,
    ) -> "list[tuple[int, dict]]":
        # #5726: accepted and ignored -- see _spilling_one_content_shaped_
        # candidate_at_a_time's own comment for why.
        return []  # every real spill_fn answer is "nothing eligible"

    result = await ctrl.force_compact_now(spill_fn=_never_spillable)

    assert result.failed, f"expected the MID_FLOOR raise to surface as failed, got {result!r}"
    # The raised UnrecoveredError itself is swallowed by force_compact_now
    # (#5633/#5708) — reach it directly via a fresh, equivalent call to
    # confirm the STRUCTURED fact the swallow discards, mirroring how
    # test_5708's own tests read `compaction_failed`/`failed` without
    # needing the exception object itself.
    from reyn.services.compaction.engine import shrink_pool_after_overflow
    pool = [{"content": "x", "spillability": "never"}]
    with pytest.raises(UnrecoveredError) as excinfo:
        shrink_pool_after_overflow(
            pool, pool, 1, spill_fn=_never_spillable, saw_byte_limit=False,
        )
    assert excinfo.value.terminal is RetryLoopTerminal.MID_FLOOR
    assert excinfo.value.spill_was_offered is True


def test_mid_floor_records_spill_was_offered_false_when_no_capability_exists():
    """Tier 2: #5717 (lead-coder BLOCKING review of #5712/PR #5716) —
    ``spill_was_offered=True`` unconditionally at MID_FLOOR was a real bug:
    a driver with NO spill mechanism at all (``spill_capability_present=
    False`` — ``PipelineExecutorDriver``'s own real shape, it carries no
    ``RouterHistoryBuffer``) would still record "spill was offered" even
    though ``spill_fn`` was never genuinely callable. "tried, found
    nothing eligible" and "there is no capability to try" are different
    facts — collapsing both onto ``spill_was_offered=True`` misrepresents
    the second case; #5717 forbids flattening them onto a bare ``False``
    fallback too (that would just move the conflation, #5699's rejected
    shape), so this test also proves ``spill_fn`` is never even CALLED
    when the capability is genuinely absent — not called-and-ignored."""
    from reyn.services.compaction.engine import shrink_pool_after_overflow

    calls: "list[list[dict]]" = []

    def _would_have_spilled_everything(offered: "list[dict]") -> "list[tuple[int, dict]]":
        calls.append(offered)
        return [(0, {**offered[0], "content": "[spilled]"})]  # never reached

    pool = [{"content": "x", "spillability": "first_choice"}]
    with pytest.raises(UnrecoveredError) as excinfo:
        shrink_pool_after_overflow(
            pool, pool, 1,
            spill_fn=_would_have_spilled_everything, saw_byte_limit=False,
            spill_capability_present=False,
        )
    assert excinfo.value.terminal is RetryLoopTerminal.MID_FLOOR
    assert excinfo.value.spill_was_offered is False, (
        "no spill capability existed — this MUST NOT read as "
        "'rung① was offered and failed'"
    )
    assert not calls, (
        "spill_fn must never be invoked when spill_capability_present is "
        "False — a real driver would have nothing to answer to"
    )


@pytest.mark.asyncio
async def test_falls_through_to_halving_when_nothing_is_spillable():
    """Tier 2: falsify pair (deny side) for the spill-alone test above —
    an otherwise-identical overflow with NOTHING spillable must fall
    through to halving (rung②): the offered SIZE shrinks across
    attempts, proving halving genuinely engages when spill cannot help,
    not that this fixture accidentally never needed it."""
    events = EventLog()
    history = _history(10)  # -> 8 middle candidates
    engine = _OverflowsUntilFewEnoughEngine(events, fits_at_or_below_chars=810)
    ctrl, _collected, hist = _make_controller(history=history, engine=engine, events=events)

    def _never_spillable(
        _offered: "list[dict]", *, seq_by_id: "dict[int, int] | None" = None,
    ) -> "list[tuple[int, dict]]":
        # #5726: accepted and ignored -- see _spilling_one_content_shaped_
        # candidate_at_a_time's own comment for why.
        return []

    result = await ctrl.force_compact_now(spill_fn=_never_spillable)
    await settle(events)

    assert engine.attempt_sizes[1:], "expected multiple shrink-and-retry attempts"
    assert engine.attempt_sizes[0] > engine.attempt_sizes[-1], (
        f"the offered SIZE must shrink via halving when spill cannot "
        f"help — got attempt sizes {engine.attempt_sizes!r}"
    )
    assert not result.failed
    summaries = [m for m in hist if m.role == "summary"]
    assert summaries, "eventual success via halving must still append a summary"


@pytest.mark.asyncio
async def test_on_demand_compaction_never_touches_recovery_episode():
    """Tier 2: acceptance ④ — ``/compact``'s own episode is recorded
    distinctly from the reactive path's, never co-mingled. The concrete,
    checkable form: ``force_compact_now``'s own ``compaction_started``
    call never opens or reads ``RouterLoopDriver``'s
    ``_recovery_episode_scope`` — this test's own controller has no
    ``RouterLoopDriver`` wired at all, and compaction still runs to
    completion, proving the on-demand path's success does not depend on
    (and therefore cannot silently share) that reactive-only concept."""
    events = EventLog()
    history = _history(6)
    engine = _OverflowsUntilFewEnoughEngine(events, fits_at_or_below_chars=400)
    ctrl, _collected, hist = _make_controller(history=history, engine=engine, events=events)
    spill_calls: "list[int]" = []

    result = await ctrl.force_compact_now(
        spill_fn=_spilling_one_content_shaped_candidate_at_a_time(spill_calls),
    )

    assert not result.failed
    assert [m for m in hist if m.role == "summary"], (
        "on-demand compaction completed with no RouterLoopDriver/"
        "recovery_episode concept present at all — the on-demand path "
        "does not depend on it, so it cannot silently share it with the "
        "reactive path either"
    )


# ---------------------------------------------------------------------------
# session.py's REAL wiring, driven end-to-end (lead-coder review, PR #5716):
# every test above calls ``ctrl.force_compact_now(spill_fn=<hand-rolled
# fake>)`` directly — none of them drive ``Session._compact_now_for_op``'s
# own ``_spill_fn`` construction (the ``getattr(self._loop_driver, "_spill_
# batch_for_retry", None)`` + ``functools.partial`` line), so the wire-shape
# adapter this PR added (``_spill_fn_adapted`` in compaction_controller.py)
# was never checked against the REAL ``RouterLoopDriver._spill_batch_for_
# retry`` it is meant to sit in front of. This section closes that gap with
# a real ``Session`` (real ``RouterLoopDriver``, real ``RouterHistoryBuffer``,
# real ``MediaStore`` — only ``litellm.acompletion`` is monkeypatched, same
# collaborator every other real-engine compaction test in this repo fakes at
# that exact boundary, e.g. ``test_slash_compact_191.py``).
# ---------------------------------------------------------------------------

_SUMMARY_JSON = json.dumps({
    "topic_arc": "compacted summary of older turns",
    "decisions": [], "pending": [],
    "session_user_facts": [], "artifacts_referenced": [],
    "new_turn_seqs": [3, 4, 5, 6],
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_real_session(tmp_path, monkeypatch):
    """A real ``Session`` with a real ``RouterLoopDriver`` and a real
    ``MediaStore`` (``multimodal_config=MultimodalConfig()`` — the default
    ``None`` in ``test_slash_compact_191.py``'s own ``_make_session`` never
    builds one, so ``spill_turn_content`` would silently no-op there; this
    test needs the genuine write). Small ``T_max`` (same technique as
    ``test_slash_compact_191.py``) so 8 large turns produce real middle
    candidates."""
    import reyn.llm.model_budget as _mb
    monkeypatch.setattr(_mb, "get_max_input_tokens", lambda model, **kw: 2800)
    return make_session(
        agent_name="default",
        budget_tracker=BudgetTracker(CostConfig()),
        state_log=StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl"),
        compaction_config=CompactionConfig(
            use_chars4_estimate=True, section_caps_spec_tokens=0,
        ),
        multimodal_config=MultimodalConfig(),
        snapshot_path=tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json",
    )


def _populate_real(session) -> None:
    for _ in range(8):
        session._append_history(ChatMessage(role="user", content="x" * 4000, ts=_now()))


def _script_overflow_until_spilled(monkeypatch, *, fits_at_or_below_chars: int) -> "list[int]":
    """Real ``litellm.acompletion`` stand-in (same boundary ``test_slash_
    compact_191.py``'s own ``_script_compaction_llm`` fakes): overflows
    while the total wire content it is handed is still full-size, succeeds
    once genuine spill (via the real ``RouterLoopDriver._spill_batch_for_
    retry`` → real ``RouterHistoryBuffer.spill_turn_content`` → real
    ``MediaStore``) has shrunk it. Records each call's total content chars
    (``call_sizes``) so the test can assert the SAME size repeated (spill
    shrinks content, never candidate count — same reasoning as
    ``_OverflowsUntilFewEnoughEngine`` above) rather than a halving
    signature."""
    call_sizes: "list[int]" = []

    async def _fake_acompletion(model, messages, **kw):
        total = sum(len(str(m.get("content", ""))) for m in messages)
        call_sizes.append(total)
        if total > fits_at_or_below_chars:
            raise _ContextLengthExceeded("context_length_exceeded")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=_SUMMARY_JSON))]
        )

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)
    return call_sizes


def test_session_wiring_drives_the_real_router_loop_driver_spill_implementation(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: ``Session._compact_now_for_op``'s own ``_spill_fn`` — built
    from the REAL ``self._loop_driver._spill_batch_for_retry`` via
    ``functools.partial(..., chain_id="manual-compact")``, exactly as
    ``session.py`` wires it — genuinely resolves a compaction-call overflow
    through real spill (not a fake standing in for it), proving this PR's
    ``_spill_fn_adapted`` translation adapter's shape is actually compatible
    with what ``RouterLoopDriver._spill_batch_for_retry`` expects/returns."""
    monkeypatch.chdir(tmp_path)
    session = _make_real_session(tmp_path, monkeypatch)
    _populate_real(session)
    # Full-size middle overflows (raw "x"*4000 turns); once real spill has
    # offloaded them to tiny path-ref previews (cap_tokens=1 forces this —
    # see spill_turn_content's own docstring), the retry fits comfortably.
    call_sizes = _script_overflow_until_spilled(monkeypatch, fits_at_or_below_chars=5_000)

    result = asyncio.run(session._compact_now_for_op())

    assert call_sizes[0] > 5_000, "the fixture's own first attempt must genuinely overflow"
    assert call_sizes[-1] <= 5_000, "the final attempt must have fit — compaction did not succeed"
    assert result["summarized_turns"] > 0
    assert any(m.role == "summary" for m in session.history)
    # The real, load-bearing evidence spill genuinely ran through the
    # PRODUCTION implementation (not just "eventually fit somehow" — e.g.
    # halving would also eventually shrink `call_sizes`, but halving on
    # `CompactionController`'s path only ever shrinks candidate COUNT,
    # never a single call's own per-turn content) — a real spill record
    # was durably appended, proving ``RouterHistoryBuffer.spill_turn_
    # content`` → ``MediaStore.save_tool_result`` actually executed.
    assert any(m.role == "spill_record" for m in session.history), (
        "expected a genuine spill_record entry from the real "
        "RouterLoopDriver._spill_batch_for_retry implementation — its "
        "absence would mean this PR's adapter silently fell through to "
        "halving instead of driving real spill"
    )


def test_session_emits_an_audit_event_when_the_loop_driver_has_no_spill_capability(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: lead-coder review (#5712, PR #5716) — a real
    ``PipelineExecutorDriver`` (production's OTHER ``ExecutionDriver``,
    ``registry.py``'s own ``compact`` op reaches it exactly like a chat
    session's) has no ``RouterHistoryBuffer`` and so never defines
    ``_spill_batch_for_retry`` at all. ``getattr(..., None)`` degrading to a
    no-op ``spill_fn`` for this case must never be SILENT — "the driver
    structurally lacks spill" is a different fact from "spill ran and found
    nothing eligible", and only an audit event lets a later reader tell a
    MID_FLOOR raised on THIS session apart from one where rung① genuinely
    ran. Compaction itself must still complete (via halving alone) — a
    missing spill capability degrades, it never blocks voluntary
    compaction."""
    from reyn.core.pipeline.work_order import PipelineWorkOrder
    from reyn.runtime.services.pipeline_executor_driver import PipelineExecutorDriver

    monkeypatch.chdir(tmp_path)
    session = _make_real_session(tmp_path, monkeypatch)
    _populate_real(session)
    assert not hasattr(PipelineExecutorDriver, "_spill_batch_for_retry"), (
        "this test's own premise — PipelineExecutorDriver never carries a "
        "RouterHistoryBuffer, so it structurally cannot implement rung①"
    )
    work_order = PipelineWorkOrder(
        run_id="r1", pipeline_name="p", pipeline={"steps": []}, input=None,
        reply_to_agent="default", reply_to_sid="main",
        driver_agent="default", driver_sid="main",
    )
    session.set_loop_driver(
        PipelineExecutorDriver(work_order, registry=None, state_log=session._state_log)
    )
    collected = collect_events(session._audit_events)
    # No spill available at all -> only halving can shrink this; a lower
    # cap than the spill-alone test above (halving only ever reduces
    # candidate COUNT, so the floor is one turn's own raw content, not a
    # tiny offloaded preview).
    call_sizes = _script_overflow_until_spilled(monkeypatch, fits_at_or_below_chars=6_500)

    result = asyncio.run(session._compact_now_for_op())
    asyncio.run(settle(session._audit_events))

    assert result["summarized_turns"] > 0, "halving alone must still resolve the overflow"
    absent_events = [e for e in collected if e.type == "compact_now_spill_capability_absent"]
    assert absent_events, (
        f"expected a compact_now_spill_capability_absent audit event — "
        f"got {[e.type for e in collected]!r}"
    )
    assert absent_events[0].data["driver_type"] == "PipelineExecutorDriver"
    assert not any(m.role == "spill_record" for m in session.history), (
        "a driver with no spill capability must never produce a spill_record"
    )
