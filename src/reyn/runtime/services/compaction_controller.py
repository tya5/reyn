"""CompactionController — synchronous head/body/tail compaction.

Extracted from Session (FP-0019 Wave 1).  Drives OS-internal compaction
(PR-N3: a direct Python helper) via :meth:`force_compact_now`.

#1128 PR-a: the background fire-and-forget path (``spawn_maybe`` →
``_maybe_compact``, the 30K-absolute ``trigger_total_tokens`` trigger) was
removed. #5528 (owner ruling, same family as #5367's elide removal): the
synchronous pre-frame guard (``ContextBudgetAdvisor.maybe_force_compact``,
estimate-based, proactive) that used to ALSO drive auto-compaction is gone
— a local token estimate cannot know what the actual provider payload will
look like, so acting on it risked compacting a conversation that would
have fit fine (#5296 decided this in principle, #5528 carried it out).
Auto-compaction is now driven solely by the ``retry_loop`` overflow
recovery path (:meth:`force_compact_now`, reached reactively on an actual
measured overflow — see ``router_loop_driver.py``'s own recovery call,
#5578: axis-agnostic since then, byte- and token-cause exhaustion alike),
plus on-demand (the ``compact`` op / ``/compact``). With no background
task, compaction always runs synchronously inside the serial router
handler.

All event emissions go through the injected ``event_log``; no silent
state changes (P6).  Business logic lives entirely here; Session
delegates via :meth:`force_compact_now` (P3).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.runtime.chat_message import is_compaction_eligible
from reyn.services.compaction.engine import (
    CompactionEngine,
    CompactionOverflowError,
    HistoryChunkToCompact,
    classify_compact_overflow,
    estimate_tokens_for_any_turn,
    select_fold_candidates_for_shortfall,
    shrink_pool_after_overflow,
    trim_head,
    trim_tail,
    wrap_summary_as_message,
)

# #1820 Part1: static reference-only preamble prepended to every rendered
# compaction summary (Hermes SUMMARY_PREFIX analog). Frames the summary as
# history — not a fresh instruction — so the model (a) treats the latest user
# message as the single source of truth and (b) does NOT re-execute `pending` /
# in-progress work after a reverse-signal (stop / undo / change of direction).
_SUMMARY_PREAMBLE = (
    "[CONTEXT SUMMARY — REFERENCE ONLY, NOT A NEW INSTRUCTION]\n"
    "The text below is a compacted summary of EARLIER conversation, kept for "
    "reference. It is history, not the current task. The most recent user message "
    "is the single source of truth: when it conflicts with anything here, follow "
    "the latest user message. If the recent conversation shows a reverse signal — a "
    "stop, an undo, or a change of direction — treat any 'pending' or in-progress "
    "work described below as CANCELLED and do not resume it unless re-requested.\n"
    "--- summary follows ---\n"
)

if TYPE_CHECKING:
    from reyn.runtime.chat_message import ChatMessage
    from reyn.services.compaction.engine import ChatSummary

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ForceCompactResult:
    """#5708 (owner real-machine incident): what :meth:`CompactionController.
    force_compact_now` actually did — the SAME ``outcome`` the ``compaction_
    check`` audit-event already carries at each of its 4 exit points, now
    also RETURNED instead of discarded. Before this, the caller
    (``Session._compact_now_for_op``) had no way to tell "genuinely nothing
    eligible" apart from "an attempt ran but folded nothing" apart from "a
    watermark that failed to advance" — all three produced the identical
    ``summarized_turns == 0`` (derived from a before/after ``covers_
    through_seq`` delta, the only signal it had), and ``/compact``'s own
    message had to hedge between causes it could not distinguish (the exact
    defect this closes).

    ``outcome`` is one of the 4 literal strings ``force_compact_now``'s own
    ``self._events.emit("compaction_check", outcome=...)`` calls use —
    passed through the SAME variable at each call site (see that method's
    body), never re-typed, so this type and the audit trail cannot drift
    apart. Deliberately NOT a single boolean (architect's #5699 ruling,
    cited again for this issue): the caller's possible questions differ —
    "should I retry now" (``already_running``), "is there really nothing
    to fold" (``forced_sync_no_turns``), "did the internal invariant hold"
    (``compaction_input_gap_invariant_violated``), "did an attempt run,
    and if so on how many candidates" (``forced_sync`` + ``candidate_
    count``) — collapsing them into one true/false would re-lose exactly
    the distinction this type exists to carry.

    ``failed`` (#5708 acceptance ④, added mid-fix on lead-coder review):
    ``_run_compaction`` raising is
    deliberately still swallowed here (#5633 — its own ``compaction_
    failed`` audit event already fired at its origin, and re-raising
    would propagate an exception past this method's own established
    no-throw contract). But "the event was emitted" is not "the caller
    was told" — audit is the observability plane, a return value is the
    control plane, and this field is what closes THAT gap: the CALLER
    (``/compact``) gets the fact "an attempt genuinely failed", never the
    exception itself, so it can say so instead of the pre-#5708 "Nothing
    was compacted this pass" a swallowed failure used to render as.
    """

    outcome: str
    candidate_count: int = 0
    batch_truncated: bool = False
    failed: bool = False


def _estimate_tokens(text: str) -> int:
    """Cheap chars/4 token estimate. Same heuristic used by other Reyn paths."""
    return max(1, len(text or "") // 4)


def _turn_to_compactor_input(
    t: "ChatMessage", *, redact: "Callable[[str], str] | None" = None,
) -> dict:
    """Serialise a ChatMessage into the compactor's ``new_turns`` shape.

    Post-PR-E1 (issue #383) the history may contain ``assistant`` entries
    with ``tool_calls``, ``tool`` entries with ``tool_call_id`` + ``name``,
    and ``user``/``assistant`` entries with multimodal ``content`` lists.
    The compactor needs enough structure to reason about tool
    activity in ``artifacts_referenced`` while staying within token caps.

    Shape we emit per turn:
      {role, text, seq, [tool_calls], [tool_call_id], [tool_name]}

    ``text`` is the derived text view (= str content or first text part
    from a list content). Tool fields are only included on the entries
    where they're set.

    FP-0050/#1822 S3 (#1820): when ``redact`` is given, the turn text is run
    through it so credential/token VALUES are stripped before they enter the
    summarizer input (and the persisted summary). ``None`` = byte-identical.
    """
    text = t.text
    if redact is not None and isinstance(text, str):
        text = redact(text)
    out: dict = {"role": t.role, "text": text, "seq": t.seq}
    if getattr(t, "tool_calls", None):
        # Compact representation: function names + arg-string lengths.
        # Avoid sending raw arg JSON since it can be large and the
        # compactor only needs the structural shape ("LLM called fn X
        # with N chars of args"). The agent's ``artifacts_referenced``
        # rule decides whether to surface the call.
        out["tool_calls"] = [
            {
                "name": (tc.get("function") or {}).get("name", ""),
                "args_chars": len((tc.get("function") or {}).get("arguments", "") or ""),
            }
            for tc in t.tool_calls
            if isinstance(tc, dict)
        ]
    if getattr(t, "tool_call_id", None):
        out["tool_call_id"] = t.tool_call_id
    if getattr(t, "name", None):
        out["tool_name"] = t.name
    return out


class CompactionController:
    """Background head/body/tail compaction service.

    Parameters
    ----------
    event_log:
        Session-scoped :class:`~reyn.core.events.events.EventLog`.  All
        compaction events are emitted here.
    config:
        :class:`~reyn.config.CompactionConfig` — thresholds and sizing.
    history_from_disk:
        Callable ``(after_seq: int) -> (list[ChatMessage], bool)`` (#4472)
        that returns conversation turns with ``seq > after_seq``, read
        DIRECTLY from the durable store (``history.jsonl``), branch-
        visibility filtered, and NEVER residency-gated — so #4387's
        resident-byte cap can never make compaction blind to content it
        hasn't actually summarized (#4470's root cause). Every call
        returns freshly-parsed ``ChatMessage`` instances from ONE source;
        this class's own :meth:`_select_candidates` excludes head/tail
        turns by Python object identity (an ``id()`` set), so mixing
        objects from a second source (e.g. a resident cache) here would
        silently defeat that exclusion — callers must never combine this
        with any other history source.

        The ``bool`` is ``truncated`` (architect's + lead-coder's #4472
        review: the read is capped PER CALL, not unbounded, so a large
        backlog is examined across multiple compaction passes rather than
        materialized in one — never claiming coverage of more than what a
        single pass actually read). ``force_compact_now`` surfaces this on
        the ``compaction_check``/``compaction_started`` audit events so a
        capped-batch pass is distinguishable from "there was genuinely
        nothing more."
    latest_summary:
        Zero-argument callable that returns the most recent ``"summary"``
        :class:`~reyn.runtime.chat_message.ChatMessage`, or ``None``.
    compaction_engine_factory:
        Zero-argument callable returning the
        :class:`~reyn.services.compaction.engine.CompactionEngine`
        that owns the single LLM call (PR-N3: OS-internal Python helper).
        #3671 follow-up: a FACTORY, not the built engine — ``CompactionEngine
        .__init__`` touches litellm's model catalog (``estimate_tokens`` /
        ``get_max_input_tokens``) to measure its budgets; calling it eagerly
        at Session construction put that cost on the TUI startup path for a
        value nothing reads until compaction actually triggers, mid-turn.
        Called AT MOST ONCE, on first reference to :attr:`_engine` (a lazy
        property, single-owner cache) — every existing reader (internal and
        the couple of external private-attribute reads this class has always
        tolerated) keeps working unchanged, since attribute access transparently
        triggers the property.
    history_appender:
        Callable ``(ChatMessage) -> None`` that appends a message to the
        persisted history.  Wraps ``Session._append_history``.
    make_summary_message:
        Callable ``(rendered_text, structured, covers_through_seq, *,
        covers_from_seq) -> ChatMessage`` that constructs the summary
        ``ChatMessage`` to be appended.  Provided by the session so the
        controller does not need to import ``ChatMessage`` or ``_now_iso``
        directly. ``covers_from_seq`` (#5765, required keyword-only —
        same shape #5759's own field addition uses): the LOWER bound of
        what this summary actually folded — see
        :func:`~reyn.runtime.chat_message.is_seq_still_active` for why a
        bare upper bound (``covers_through_seq`` alone, the pre-#5765
        shape) silently hid head-protected turns that were never folded.
    render_summary:
        Callable ``(structured: dict) -> str`` that renders a structured
        summary dict to a storage-friendly text blob.
    """

    def __init__(
        self,
        *,
        event_log: EventLog,
        config: CompactionConfig,
        history_from_disk: Callable[[int], "tuple[list[ChatMessage], bool]"],
        latest_summary: Callable[[], ChatMessage | None],
        compaction_engine_factory: "Callable[[], CompactionEngine]",
        history_appender: Callable[[ChatMessage], None],
        make_summary_message: Callable[..., ChatMessage],
        render_summary: Callable[[dict], str],
        # FP-0050/#1822 S3 (#1820): content-threat scan config. When enabled,
        # turn text is secret-redacted before entering the summarizer input.
        # None (test paths) → no redaction (byte-identical).
        threat_scan: "object | None" = None,
    ) -> None:
        self._events = event_log
        self._config = config
        self._threat_scan = threat_scan
        self._history_from_disk = history_from_disk
        self._latest_summary = latest_summary
        self._compaction_engine_factory = compaction_engine_factory
        self.__engine_cache: "CompactionEngine | None" = None
        self._append_history = history_appender
        self._make_summary_message = make_summary_message
        self._render_summary = render_summary
        self._compacting: bool = False

    @property
    def is_compacting(self) -> bool:
        """#5588: is a compaction pass currently in flight — the ONE real,
        zero-fabrication signal this PR's TUI progress display is built on.
        A thin public read of the existing ``_compacting`` guard (already
        set/cleared around :meth:`force_compact_now`'s own
        ``_run_compaction`` call, above) — no new state, just a public
        accessor for state that already existed private-only until now."""
        return self._compacting

    @property
    def _engine(self) -> CompactionEngine:
        """The compaction engine, built via the factory on first reference
        and cached (single owner, computed at most once — #3671 follow-up).

        A property, not a plain attribute: every existing reader — internal
        methods below, and the couple of call sites elsewhere in this
        package that read ``controller._engine`` directly (a private
        attribute, tolerated pre-existing style) — keeps working with no
        change, since attribute access transparently triggers this.

        ``None`` (not a separate sentinel, unlike ``RouterHostAdapter``'s
        ``_TURN_BUDGET_ENGINE_UNSET``) means "not built yet" here — safe
        because, unlike ``TurnBudgetEngine`` (whose factory can legitimately
        return ``None`` for a tiny-context model that cannot support
        force-close), ``CompactionEngine``'s factory has no "built but
        absent" case: every constructed engine is real, so ``None`` has
        exactly one meaning and mypy narrows it without a cast."""
        if self.__engine_cache is None:
            self.__engine_cache = self._compaction_engine_factory()
        return self.__engine_cache

    def rebuild_engine(self) -> None:
        """Discard the cached engine so the factory runs again on next
        reference (#3785: a ``/model`` switch changes the model the factory
        resolves against — compaction now always follows the conversation
        model, so the cached engine goes stale the moment ``/model`` runs).

        Mirrors ``RouterHostAdapter.set_turn_budget_engine`` for the sibling
        engine, but stays LAZY rather than rebuilding eagerly here: the
        SAME factory this controller was constructed with reads
        ``Session.model`` fresh each call, so invalidating the cache is
        enough — consistent with #3671's "don't touch litellm until
        actually needed" (a ``/model`` switch that never triggers
        compaction again should not pay to rebuild it)."""
        self.__engine_cache = None

    # ── internal compaction logic ─────────────────────────────────────────────

    def _select_candidates(
        self,
        turns: "list[ChatMessage]",
        prev_cover: int,
    ) -> "list[ChatMessage]":
        """Select compaction candidates: token-budget HEAD/TAIL protect,
        the SHORTFALL against ``main_M_room`` selects.

        #5719 (architect ruling, real-machine incident: #5712's own fix
        compacted 1.6M raw_middle chars to a ~3K summary against a 950K
        window — needing only a ~650K reduction, 600x less than what
        actually folded). head/tail (unchanged since #1128 step 3 —
        token-budget trimming via the engine's ``ComputedBudgets``)
        answer ONE question: is this turn protected. Everything strictly
        between them, with ``seq > prev_cover`` (not yet covered by the
        latest summary), used to become a fold CANDIDATE unconditionally
        — collapsing "not protected" and "must be folded" onto one
        predicate (architect's own naming of the defect). They are now
        two separate steps: the unprotected-and-uncovered set is
        computed exactly as before, then :func:`select_fold_candidates_
        for_shortfall` selects only as much of it (oldest first, group-
        aware) as is needed to bring the REMAINING unprotected middle
        back under ``main_M_room`` — never "all of it" by default. See
        that function's own docstring for the selection algorithm and
        why no slack constant is added here.

        Falls back to a quarter of get_max_input_tokens when budgets are
        None (engine not yet initialised — highly unlikely in
        production but safe); ``main_M_room`` has no real formula to
        fall back to in that branch (it needs T_SP/new_msg_budget, which
        this fallback never had), so it re-derives the same "room after
        head+tail" shape the real formula uses, from values already in
        scope here.
        """
        budgets = getattr(self._engine, "budgets", None)
        model = getattr(self._engine, "_model", "")
        use_chars4 = getattr(self._config, "use_chars4_estimate", False)
        if budgets is not None:
            head_budget = budgets.head_budget
            tail_budget = budgets.tail_budget
            main_M_room = budgets.main_M_room
        else:
            from reyn.llm.model_budget import get_max_input_tokens
            fallback = get_max_input_tokens(model) if model else 100_000
            head_budget = tail_budget = fallback // 4
            main_M_room = fallback - head_budget - tail_budget

        head_turns = trim_head(turns, head_budget, model, use_chars4=use_chars4)
        tail_turns = trim_tail(turns, tail_budget, model, use_chars4=use_chars4)
        head_id_set = {id(t) for t in head_turns}
        tail_id_set = {id(t) for t in tail_turns}
        unprotected = [
            t for t in turns
            if id(t) not in head_id_set
            and id(t) not in tail_id_set
            and t.seq > prev_cover
        ]
        unprotected_tokens = sum(
            estimate_tokens_for_any_turn(t, model, use_chars4=use_chars4) for t in unprotected
        )
        shortfall = unprotected_tokens - main_M_room
        return select_fold_candidates_for_shortfall(
            unprotected, shortfall, model, use_chars4=use_chars4,
        )

    async def force_compact_now(
        self, *,
        # #5726: widened from Callable[[list[dict]], ...] -- the real
        # production spill_fn (RouterLoopDriver._spill_batch_for_retry,
        # bound via functools.partial(..., chain_id=...)) requires a
        # seq_by_id kwarg _spill_fn_adapted (below) now always supplies,
        # freshly computed per shrink attempt (see that function's own
        # comment for why it cannot be pre-bound the way chain_id is).
        # `...` (not a stricter Protocol) matches this file's own existing
        # understated-type precedent for this parameter (chain_id was
        # ALREADY invisible to the declared type, bound via partial,
        # before this widening).
        spill_fn: "Callable[..., list[tuple[int, dict]]]",
        spill_capability_present: bool = True,
    ) -> ForceCompactResult:
        """Synchronous force-trigger — single pass (#1128 PR-c).

        #5712 (owner ruling, "operator の compact 要求は spill 含む縮小
        フローだから"): ``spill_fn`` is now REQUIRED — no path through
        this method may run the mid-side shrink ladder without rung①
        (spill) genuinely available (acceptance ②: no ``None`` left at
        this boundary). Both real callers (``Session._compact_now_for_
        op``, ``RouterLoopDriver``'s own post-``retry_loop``-exhaustion
        fallback) pass the SAME concrete spill implementation
        (``RouterLoopDriver._spill_batch_for_retry``) — one real
        implementation, reused, never a second copy for this path.

        ``spill_capability_present`` (#5717): default ``True`` — the
        caller genuinely has a driver-backed ``spill_fn``. ``Session.
        _compact_now_for_op`` passes ``False`` when the attached driver
        has no spill mechanism at all (e.g. ``PipelineExecutorDriver``)
        — the ``spill_fn`` it passes in that case is still a real,
        required, non-``None`` no-op lambda (acceptance ②, unaffected),
        but ``shrink_pool_after_overflow`` must not call it and must not
        record a MID_FLOOR raise as "spill was offered" when there was
        no capability to offer it with. See that function's own
        docstring for the full "one value, two facts" reasoning.

        #5528: the pre-frame guard that used to call this proactively, on
        an ESTIMATE, is gone — this is now reached only reactively (a real
        overflow the router's own LLM call actually raised —
        ``router_loop_driver.py``'s byte-limit recovery path) or on-demand
        (``/compact`` / the ``compact`` op). Emits ``compaction_check``
        with ``outcome="forced_sync"``.

        #5708: RETURNS a :class:`ForceCompactResult` naming which of the 4
        outcomes fired, instead of ``None`` — every exit point below both
        emits the audit event AND returns the matching result from the
        SAME outcome literal (never two separately-typed copies of the
        same fact). See that class's own docstring for why the caller
        needs the full outcome, not a collapsed boolean.

        #1128 PR-c: collapsed from the former Option-B race-recovery loop
        (``max_passes`` re-measure + ``ForceCompactRaceUnrecoveredError``) to a
        single pass. That loop existed to re-run when another coroutine appended
        to history mid-compaction. Cross-driver turn serialization is now
        structural — every transport that drives ``run_one_iteration`` holds the
        shared per-agent lock (PR-b, ``reyn.runtime.agent_locks``), and within a
        turn ``_append_history`` is synchronous — so no concurrent append can
        land during this method. If the single pass under-shoots (the guard's
        estimate under-counted), the ``retry_loop`` overflow backstop in
        ``_run_router_loop`` folds raw_middle and monotonically shrinks: that is
        the under-shoot floor, replacing the multi-pass-or-raise contract.

        #1128 PR-a: the former vestigial ``compaction_lock`` acquire was
        removed — only this method acquired it; no history appender awaited it.
        Cross-driver turn serialization is the shared per-agent lock's job (PR-b).
        """
        if self._compacting:
            outcome = "already_running"
            self._events.emit("compaction_check", outcome=outcome)
            return ForceCompactResult(outcome=outcome)

        latest = self._latest_summary()
        prev_cover = (latest.meta or {}).get("covers_through_seq", 0) if latest else 0
        # #4472: read the candidate INPUT from the durable store
        # (history.jsonl), never residency-gated — see
        # Session._durable_active_history_after's own docstring. This is
        # the structural fix for #4470 (a resource-role eviction was
        # silently deciding a semantic-role question — whether content had
        # been summarized): residency now has NO influence on what
        # compaction considers, so the "gap" #4470/#4471 had to detect and
        # skip around cannot occur anymore — there is nothing left for a
        # gap to be a gap IN.
        #
        # `batch_truncated` (architect's + lead-coder's #4472 review): the
        # durable read is capped PER CALL (bounded MATERIALIZATION, not
        # bounded EXAMINATION — #4470 forbids skipping unseen content, not
        # reading a contiguous prefix of it per call). A large backlog is
        # covered across multiple compaction passes; this pass's own
        # `covers_through_seq` (below, `candidates[-1].seq`) already only
        # ever reflects what THIS batch actually contained — surfaced on
        # the audit trail so a capped-batch pass is distinguishable from
        # "there was genuinely nothing more to compact."
        history, batch_truncated = self._history_from_disk(prev_cover)
        # #5699 (owner real-machine incident): a role="system"/Disclosure.MODEL
        # entry has been admitted into the live WINDOW since #5678/#5688
        # (router_history_buffer.py's own _elide_candidate_turns and
        # decompose_history_for_retry, both via chat_message.py's
        # is_compaction_eligible[_including_summary]) but this candidate
        # filter never gained the same admission — such an entry could
        # accumulate in the window forever, permanently un-foldable by
        # /compact (this method) even though the reactive overflow
        # ladder's own raw_middle DOES include it (decompose_history_for_
        # retry). Same shared predicate as those two call sites, imported
        # rather than re-derived, so this can never drift from them again.
        turns = [m for m in history if is_compaction_eligible(m)]
        if not turns:
            outcome = "forced_sync_no_turns"
            self._events.emit("compaction_check", outcome=outcome)
            return ForceCompactResult(outcome=outcome)
        # #4472 architect review, point ③: NOT a normal branch — a defensive
        # invariant, not a routine outcome. The durable read always starts
        # its batch immediately after `prev_cover` (only the END of the
        # batch is capped, never the beginning skipped), so `turns`'s
        # earliest real seq should always be exactly `prev_cover + 1` (or
        # the file's own first entry, if prev_cover is 0). A hit here means
        # something ELSE narrowed the durable read out from under this
        # method — #4470's silent-coverage-claim defect would otherwise
        # reopen through that new path. Kept as a LOUD, named audit outcome
        # (not a silent skip) so a future regression that reintroduces a
        # bound is caught immediately, not rediscovered the way #4470
        # itself was.
        resident_seqs = [t.seq for t in turns if t.seq > 0]
        if resident_seqs and min(resident_seqs) > prev_cover + 1:
            outcome = "compaction_input_gap_invariant_violated"
            self._events.emit("compaction_check", outcome=outcome)
            return ForceCompactResult(outcome=outcome)
        candidates = self._select_candidates(turns, prev_cover)

        outcome = "forced_sync"
        self._events.emit(
            "compaction_check", outcome=outcome,
            batch_truncated=batch_truncated,
            candidate_count=len(candidates),
        )
        if not candidates:
            return ForceCompactResult(
                outcome=outcome, candidate_count=0, batch_truncated=batch_truncated,
            )

        self._compacting = True
        failed = False
        try:
            await self._run_compaction(
                candidates, latest, spill_fn=spill_fn,
                spill_capability_present=spill_capability_present,
            )
        except Exception:
            # #5633 (lead-coder review): NOT re-raised, and NOT re-emitted
            # here — whatever `_run_compaction` raises has already had its
            # own `compaction_failed` emitted at its own origin
            # (`CompactionEngine.compact()`'s own except for a compact()
            # failure, or `_run_compaction`'s own post-processing
            # `try`/`except` for anything after `compact()` returns; see
            # `_run_compaction`'s own comment for why those two `try`
            # blocks never overlap). Swallowing the exception itself
            # stays correct (`force_compact_now`'s own no-throw contract
            # is intentional, #5633) — #5708's own finding is narrower:
            # "the event was emitted" is not "the caller was told", so
            # `failed` below carries the ONE bit of fact the caller
            # actually needs (an attempt genuinely failed) without
            # propagating the exception object itself.
            failed = True
        finally:
            self._compacting = False
        return ForceCompactResult(
            outcome=outcome, candidate_count=len(candidates),
            batch_truncated=batch_truncated, failed=failed,
        )

    async def persist_recovery_summary(
        self, chat_summary: "ChatSummary", *, covers_through_seq: int,
    ) -> None:
        """#5578 (architect ruling, issuecomment on #5578) — persist a
        ``ChatSummary`` that a SUCCESSFUL overflow-recovery already
        produced and used, without triggering a new compaction LLM call.

        Why not ``force_compact_now``: the fold already happened inside
        ``retry_loop`` (``engine.py``) — the model already answered
        against that folded history. Calling ``force_compact_now`` here
        would spend a SECOND compaction LLM call (real money) re-folding
        content that is already folded, and (#5296's own line) trigger a
        NEW irreversible compaction step — this method triggers none:
        ``self._engine.compact()`` is deliberately never called here, only
        the existing summary/append/event machinery ``_run_compaction``
        already uses for its own tail half.

        ``covers_through_seq`` is the CALLER's own responsibility, never
        read off ``chat_summary.covers_through_seq`` — that field is
        structurally ``0`` for a ``ChatSummary`` retry_loop produced (its
        own fold runs on wire dicts with no real ``seq`` — see
        ``SeqUnavailable.WIRE_DICTS_CARRY_NO_SEQ``'s docstring, #5498).
        #5498's own comment names exactly this scenario ("a future change
        that... starts persisting retry_loop's own summary") as the
        thing that needs the real value re-derived before persisting —
        the caller (``router_loop_driver.py``) derives it from
        ``decompose_history_for_retry``'s own ``seq_by_id`` id()-keyed
        map, never from this object's own field.

        Idempotent (architect's own deliverable ①): if the durable
        watermark already covers ``covers_through_seq``, this is a
        no-op — a stale or duplicate call must never regress or
        re-persist an already-durable watermark.
        """
        if covers_through_seq <= 0:
            # #5498's own guard, re-applied here: a bogus/unavailable
            # covers_through_seq (the caller failed to derive a real one,
            # or nothing was actually folded) must never reach
            # history.jsonl.
            self._events.emit(
                "recovery_summary_persisted", outcome="no_covers_through_seq",
                covers_through_seq=covers_through_seq,
            )
            return

        latest = self._latest_summary()
        prev_cover = (latest.meta or {}).get("covers_through_seq", 0) if latest else 0
        if covers_through_seq <= prev_cover:
            self._events.emit(
                "recovery_summary_persisted", outcome="already_covered",
                covers_through_seq=covers_through_seq, prev_cover=prev_cover,
            )
            return

        structured = {**chat_summary.to_dict(), "covers_through_seq": covers_through_seq}
        rendered = _SUMMARY_PREAMBLE + self._render_summary(structured)
        # #5765: this writer's own fold has NO head-protected exclusion —
        # `retry_loop`'s ladder decomposes the wire dicts directly
        # (`decompose_history_for_retry`), never applies `trim_head`'s
        # own token-budget protection the way `_run_compaction`'s
        # candidate selection does — so the range it actually covered IS
        # `(prev_cover, covers_through_seq]`, with no gap. `covers_from_
        # seq = prev_cover + 1` records that EMPTY excluded region
        # explicitly (architect ruling, #5765 co-vet) rather than leaving
        # the field unset — an unset field means "unknown, protect
        # everything" (see `is_seq_still_active`'s own SAFE-SIDE
        # fallback), which would be WRONG here: this writer's own range
        # genuinely has nothing to protect.
        summary_msg = self._make_summary_message(
            rendered, structured, covers_through_seq,
            covers_from_seq=prev_cover + 1,
        )
        self._append_history(summary_msg)
        self._events.emit(
            "recovery_summary_persisted", outcome="persisted",
            covers_through_seq=covers_through_seq,
            section_lengths={
                k: len(v) if isinstance(v, list) else len(str(v))
                for k, v in structured.items()
                if k != "covers_through_seq"
            },
        )

    async def _run_compaction(
        self,
        candidates: list[ChatMessage],
        previous_summary: ChatMessage | None,
        *,
        # #5726: widened -- see force_compact_now's own comment on this
        # same parameter name above.
        spill_fn: "Callable[..., list[tuple[int, dict]]]",
        spill_capability_present: bool = True,
    ) -> None:
        """Call the compaction engine and persist the resulting summary entry."""
        cfg = self._config
        # #5633 (lead-coder review, the gap the first restructure left
        # silent): this setup segment — building prev_structured, redact,
        # the input_chunk's own list comprehension over `candidates` — runs
        # BEFORE compact() is ever called, so compaction_started has not
        # fired yet on THIS attempt; before this PR, force_compact_now's
        # own catch-all still emitted `compaction_failed` for a failure
        # here (an observability regression this PR would otherwise have
        # introduced, not merely a dangling-marker gap). Its own try/except
        # keeps compact() itself the only call outside any local try (see
        # the comment just above that call).
        try:
            prev_structured: dict | None = None
            if previous_summary is not None:
                meta = previous_summary.meta or {}
                structured = meta.get("structured")
                if isinstance(structured, dict):
                    prev_structured = structured
                    # carry forward the prior covers_through_seq for continuity
                    if "covers_through_seq" not in prev_structured:
                        prev_structured = {
                            **prev_structured,
                            "covers_through_seq": meta.get("covers_through_seq", 0),
                        }

            # FP-0050/#1822 S3 (#1820): strip credential/token values from turn text
            # before it enters the summarizer input (so secrets aren't baked into the
            # persisted summary). Gated by threat_scan.enabled; None/disabled → no-op.
            _ts = self._threat_scan
            _redact = None
            if _ts is not None and getattr(_ts, "enabled", True):  # #4523: shadow default matches ThreatScanConfig.enabled's own declared True
                from reyn.security.secret_redaction import redact_secrets
                _redact = redact_secrets
            # #5531 condition③: this caller is always tail-side — `candidates`
            # comes from `_select_candidates(turns, prev_cover)`, which only
            # ever returns turns chronologically AFTER `prev_cover` (the prior
            # summary's own covers_through_seq) — so the order is always
            # summary-then-new-turns, never the reverse (that only happens in
            # retry_loop's own head-shrink path, engine.py).
            _summary_messages = (
                [wrap_summary_as_message(prev_structured)] if prev_structured else []
            )
            # #5721 (architect ruling, #5712's own "reactive protected /
            # operator-driven bare" asymmetry — this is its 3rd instance):
            # this is the OPERATOR-DRIVEN path (`/compact` -> force_compact_
            # now -> here) — before this fix it built section_token_caps
            # from `cfg.section_token_caps.*`, the STATIC legacy defaults
            # (sum 1500 tokens), regardless of the model's own context
            # window. `compute_budgets`'s own `section_caps` field (window-
            # relative, computed from `section_weights`) is PRIMARY —
            # engine.py's own comment names the static values "Fallback:
            # use CompactionSectionCaps legacy values" for when weights are
            # absent — and `engine.py`'s reactive `_stage_fold` (the
            # retry_loop-internal compact() call) already reads it that
            # way (`self._bg.section_caps if self._bg.section_caps else
            # {...legacy...}`). This path now reads the SAME
            # `self._engine.budgets.section_caps`, matching that shape —
            # never a second, independently-derived cap dict.
            budgets = getattr(self._engine, "budgets", None)
            section_caps = (
                budgets.section_caps
                if budgets is not None and budgets.section_caps
                else {
                    "topic_arc": cfg.section_token_caps.topic_arc,
                    "decisions": cfg.section_token_caps.decisions,
                    "pending": cfg.section_token_caps.pending,
                    "session_user_facts": cfg.section_token_caps.session_user_facts,
                    "artifacts_referenced": cfg.section_token_caps.artifacts_referenced,
                }
            )
            input_chunk = HistoryChunkToCompact(
                messages=_summary_messages
                + [_turn_to_compactor_input(t, redact=_redact) for t in candidates],
                section_token_caps=section_caps,
            )
        except Exception as exc:
            # #5633: this segment's OWN failure surface — see the comment
            # above the try for why it needs one (compaction_started has
            # not fired yet here, but the pre-#5633 catch-all DID emit for
            # this segment, so omitting this try/except would be a
            # regression, not merely a non-issue).
            self._events.emit("compaction_failed", error=str(exc))
            raise

        # #5475 (architect ruling): compaction_started now emits at
        # CompactionEngine.compact()'s own entry — the one real entry both
        # of its callers (this method, and retry_loop's own internal
        # compaction attempts) share — not here. Moved, not duplicated
        # (the old emit here is deleted, not left alongside the new one;
        # see #5382/#5455 for why two emit sites for the same kind is
        # rejected). This caller's own real `seq` (`candidates[-1].seq` —
        # unlike retry_loop's wire-dict turns, `_turn_to_compactor_input`
        # keeps `seq` per turn) is passed through explicitly.
        # #5712 (owner ruling, "operator の compact 要求は spill 含む縮小
        # フローだから"): #5633's own "deliberately OUTSIDE any try/except"
        # structure below is UNCHANGED in spirit — a compact() failure is
        # still fully handled at its own origin (CompactionEngine.
        # compact()'s own except emits `compaction_failed` and re-raises)
        # — this loop only adds the SAME rung①(spill)+rung②(halve) shrink
        # retry `RecoveryLadder` already runs on a compact()-origin
        # OVERFLOW (`engine.classify_compact_overflow`/`shrink_pool_
        # after_overflow`, #5712 — the ONE shared implementation, not a
        # second copy). A FATAL/RETRYABLE classification still propagates
        # bare, immediately, exactly as it always did (`classify_compact_
        # overflow`'s own `raise exc` for those, uncaught here). Only a
        # genuinely shrinkable OVERFLOW gets a shrink-and-retry; the loop
        # itself never swallows or re-emits anything `compact()`'s own
        # `except` did not already emit.
        #
        # `pool` is the FULL wire-dict list `input_chunk` already built
        # (summary-then-candidates, #5531 condition③'s own ordering,
        # unchanged) — the shared function mutates it in place via spill.
        # `_n_summary` messages never shrink via halving (this caller's
        # own candidate selection already excluded head/tail; the prior
        # summary, if any, is the one thing NOT drawn from `candidates`)
        # — attempt_len only ever trims from the CANDIDATE side, matching
        # `covers_through`'s own derivation below.
        pool = input_chunk.messages
        _n_summary = len(_summary_messages)
        attempt_len = len(pool)
        _last_saw_byte_limit = False
        # #5712 (found while wiring this in, not assumed): `_turn_to_
        # compactor_input`'s own wire shape (`text`, no `spillability`)
        # is a DIFFERENT convention from the one `spill_fn`'s real
        # implementation (`RouterLoopDriver._spill_batch_for_retry` ->
        # `_spill_batch_within_face`) expects (`content` + `spillability`
        # — retry_loop's own wire-dict turns already carry both, via
        # `decompose_history_for_retry`). Passing this caller's own
        # `text`-shaped dicts to it directly would make EVERY candidate
        # look ineligible (`t.get("content")` always `None`) — spill
        # would silently never fire, the exact silent-half-fix #5712
        # itself is about. `_turn_to_compactor_input`'s own output shape
        # stays UNCHANGED (existing tests assert exact dict equality on
        # it, `test_compaction_controller_tool_aware.py`) — this adapter
        # translates at the boundary instead: builds a `content`+
        # `spillability`-shaped VIEW for the real spill_fn, then
        # translates any returned edit back into this caller's own
        # `text` field before `shrink_pool_after_overflow` applies it to
        # `pool`.
        _spillability_by_index = (
            [""] * _n_summary + [c.spillability.value for c in candidates]
        )

        def _spill_fn_adapted(offered: "list[dict]") -> "list[tuple[int, dict]]":
            content_shaped = [
                {**item, "content": item.get("text", ""), "spillability": _spillability_by_index[i]}
                for i, item in enumerate(offered)
            ]
            # #5720/#5725 -> #5726: RouterLoopDriver._spill_batch_for_retry
            # (spill_fn's real production implementation, bound via
            # functools.partial(..., chain_id="manual-compact") in
            # session.py's own _compact_now_for_op) made seq_by_id a
            # REQUIRED kwarg — #5725 wired it for retry_loop's own
            # reactive spill_fn partial (built with the real seq_by_id
            # already in hand at THAT construction time), but left this
            # (operator-driven /compact) call site unwired -> TypeError,
            # the 5th instance of #5712's own "reactive protected /
            # operator-driven bare" asymmetry this arc keeps finding.
            #
            # Unlike the reactive path, seq_by_id cannot be baked into
            # session.py's own partial at construction time — content_
            # shaped is rebuilt fresh, per shrink attempt, only here.
            # But `_turn_to_compactor_input` (the wire-shape builder that
            # produced `item` before this adapter ever saw it) already
            # stamps a real `"seq"` field on every turn (this repo's own
            # `new_turns` shape, `{role, text, seq, ...}`) — no side
            # channel is needed the way the reactive path's own decompose
            # step required one; the real seq is already sitting on each
            # dict, unlike `decompose_history_for_retry`'s wire dicts.
            seq_by_id = {id(d): d["seq"] for d in content_shaped}
            edits = spill_fn(content_shaped, seq_by_id=seq_by_id)
            return [
                (idx, {**offered[idx], "text": replacement.get("content", offered[idx].get("text", ""))})
                for idx, replacement in edits
            ]
        # #5765: the range's own LOWER bound — the first candidate this
        # cycle actually offers to the summarizer. Invariant across shrink
        # attempts (`shrink_pool_after_overflow` only ever trims `attempt_
        # len` — i.e. the END of `pool` — never removes `candidates[0]`
        # itself as long as `_n_candidates_offered > 0`), so it is
        # computed once, outside the retry loop below, unlike `_covers_
        # through` (which genuinely changes per shrink attempt).
        _covers_from = candidates[0].seq
        while True:
            offered = pool[:attempt_len]
            _offered_chunk = HistoryChunkToCompact(
                messages=offered, section_token_caps=input_chunk.section_token_caps,
            )
            # `covers_through` names the LAST candidate actually offered
            # this attempt — never `candidates[-1].seq` unconditionally
            # (#5531 condition③'s own contract holds only for a full,
            # un-shrunk attempt; a shrunk one covers a genuine PREFIX,
            # same shape `RecoveryLadder`'s own partial-fold-then-continue
            # already has for the reactive path — a later `/compact` call
            # covers the remainder, this method staying "single pass").
            _n_candidates_offered = max(attempt_len - _n_summary, 0)
            _covers_through = (
                candidates[_n_candidates_offered - 1].seq
                if _n_candidates_offered > 0 else candidates[0].seq
            )
            try:
                chat_summary = await self._engine.compact(
                    _offered_chunk, covers_through=_covers_through,
                )
                break
            except Exception as exc:
                try:
                    classify_compact_overflow(exc)
                except CompactionOverflowError as _overflow_exc:
                    _last_saw_byte_limit = (
                        getattr(_overflow_exc.__cause__, "status_code", None) == 413
                    )
                    attempt_len = shrink_pool_after_overflow(
                        pool, offered, attempt_len,
                        spill_fn=_spill_fn_adapted, saw_byte_limit=_last_saw_byte_limit,
                        spill_capability_present=spill_capability_present,
                    )
                    continue
                raise  # FATAL/RETRYABLE — bare, unchanged (#5633)
        new_turn_count = _n_candidates_offered
        try:
            structured = chat_summary.to_dict()
            covers = chat_summary.covers_through_seq or _covers_through
            # #1820 Part1: frame the rendered summary with a static reference-only
            # preamble (Hermes SUMMARY_PREFIX analog) so the model treats the summary as
            # history — NOT a fresh instruction — and does not re-execute `pending` work
            # after a reverse-signal (stop / undo / change of direction). Prepended here
            # (controller-owned, render-fn-independent) so every rendered summary carries
            # it. Static string → no LLM dependency.
            rendered = _SUMMARY_PREAMBLE + self._render_summary(structured)

            summary_msg = self._make_summary_message(
                rendered, structured, covers, covers_from_seq=_covers_from,
            )
            self._append_history(summary_msg)
            self._events.emit(
                "compaction_completed",
                new_turn_count=new_turn_count,
                covers_through_seq=covers,
                section_lengths={
                    k: len(v) if isinstance(v, list) else len(str(v))
                    for k, v in structured.items()
                    if k != "covers_through_seq"
                },
                # #4703 axis①: the compact() LLM call's own real usage — see
                # ChatSummary's own docstring for why it lives there and not in
                # ``structured``/``to_dict()`` (never persisted to history.jsonl).
                # None only when usage genuinely could not be read off the
                # response — never coerced to 0.
                prompt_tokens=chat_summary.prompt_tokens,
                completion_tokens=chat_summary.completion_tokens,
                cost_usd=chat_summary.cost_usd,
            )
        except Exception as exc:
            # #5633: this method's OWN failure surface — everything AFTER
            # compact() returns a real ChatSummary (rendering, persisting,
            # emitting compaction_completed). compact() itself is outside
            # this try (see the comment above it) so its own failures can
            # never reach here — this except only ever fires for a
            # genuinely NEW failure, never the same one compact() already
            # emitted `compaction_failed` for.
            self._events.emit("compaction_failed", error=str(exc))
            raise


__all__ = ["CompactionController"]
