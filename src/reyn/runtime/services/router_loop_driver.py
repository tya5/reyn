"""RouterLoopDriver — per-turn router loop orchestration for Session.

Owns:

  - run_turn(user_text, chain_id)
  - _run_with_shrink(loop, text)
  - _check_cap(user_text)
  - is_cancel_requested()
  - request_cancel()               — turn-cancel seam (called by cancel_inflight)

Cancel lifecycle (#1468): the cooperative-cancel flag lives here.
``request_cancel()`` is called by Session.cancel_inflight() for the turn
piece; ``is_cancel_requested()`` is polled at each run_loop iteration via the
RouterHostAdapter.turn_cancel_fn callback wired in Session.__init__.
"""
from __future__ import annotations

import asyncio
import contextlib
from functools import partial as _partial
from typing import TYPE_CHECKING, Any, Callable

from reyn.runtime.chat_message import Spillability
from reyn.services.compaction.engine import SUMMARY_MESSAGE_ROLE
from reyn.services.compaction.engine import RetryPayload as _RetryPayload
from reyn.services.compaction.engine import is_shrinkable_overflow as _is_shrinkable_overflow
from reyn.services.compaction.engine import wire_role as _wire_role

if TYPE_CHECKING:
    from reyn.config.chat import SafetyConfig
    from reyn.services.compaction.engine import ChatSummary


#: #5599 follow-up — the exhaustive set of keys a real chat-completions
#: wire message may carry, enumerated directly from `_serialise_turn`'s
#: own construction (router_history_buffer.py — `role`/`content` always,
#: `tool_calls`/`tool_call_id`/`name` when the ChatMessage carries them)
#: plus the 3 reasoning-continuity fields `_REASONING_BUNDLE_FIELDS`
#: (reasoning_continuity.py) attaches. An ALLOW-list, not a deny-list —
#: see `_router_main_call`'s own comment for why (a deny-list already
#: missed one real internal key, `wrap_summary_as_message`'s own
#: `**summary` spread, alongside the `spillability` key it did know
#: about).
_WIRE_MESSAGE_KEYS = (
    "role", "content", "tool_calls", "tool_call_id", "name",
    "reasoning_content", "thinking_blocks", "provider_specific_fields",
)


def _narrowing_per_iteration(safety: "SafetyConfig") -> bool:
    """Whether ``safety.threat_scan.capability_narrowing`` is at the ``iteration``
    rung (#3501). Delegates to the config object's own predicate rather than
    comparing the string here, so the vocabulary lives in exactly one place.

    #4525: direct attribute access, not a ``getattr`` guard. The prior
    docstring claimed the guard existed because "test hosts pass partial
    safety objects" — lead-coder's grep found this was true of exactly ONE
    test host (which has since been fixed to construct a real
    ``SafetyConfig()``, matching what production has ALWAYS passed:
    ``session.py``'s own ``safety or SafetyConfig()`` normalization means
    ``RouterLoopDriver``'s one real construction site, ``session.py:1471``,
    never sees anything but a fully-populated ``SafetyConfig``, whose own
    ``threat_scan`` field is never ``None`` by construction
    (``field(default_factory=ThreatScanConfig)``). A ``getattr`` guard
    protecting a shape nothing ever constructs is the shape CLAUDE.md's
    test-review Q3 calls out — the fix is removing the guard, not adding a
    test for it, per architect's revised call after the premise was
    falsified. A caller genuinely passing a wrong type now gets a loud
    ``AttributeError`` instead of a silent ``False``."""
    return bool(safety.threat_scan.narrowing_per_iteration())


class RouterLoopDriver:
    """Orchestrates the per-turn router loop for one Session.

    Constructed once per Session; all stateful orchestration that previously
    lived inline in Session._run_router_loop is concentrated here.
    """

    def __init__(
        self,
        *,
        router_host: Any,             # RouterHostAdapter
        safety: Any,                  # SafetyConfig — loop.max_tool_calls_per_turn
        router_max_iterations: int,
        budget_tracker: Any,          # BudgetTracker — for RouterLoop
        non_interactive: bool,
        exclude_tools: Any,           # set — for RouterLoop
        excluded_categories: Any = frozenset(),  # #1667 set — catalog categories for RouterLoop
        contextual_permission: Any = None,  # #1827 S3 ContextualPermission — for RouterLoop live tool gate; None = no narrowing
        contextual_for_turn_fn: Any = None,  # #1827 S4b: () -> ContextualPermission|None — per-turn effective contextual (context-auto); None → static contextual_permission (byte-identical)
        budget: Any,                  # BudgetGateway — cap + usage accounting
        resolver: Any,                # LLMResolver — model resolution
        compaction: Any,              # CompactionConfig — retry_loop cfg
        compaction_controller: Any,   # CompactionController — retry_loop engine
        token_learner: Any,           # TokenMultiplierLearner — retry_loop learner
        events: Any,                  # EventLog — emit events
        model_override_fn: "Callable[[], str | None]",  # () -> _model_override (None = unset)
        history_buffer: Any,          # RouterHistoryBuffer — history + SP
        budget_advisor: Any,          # ContextBudgetAdvisor — enforce_new_msg_budget
        limit_checkpoint_fn: Callable,  # async; Session._handle_chat_limit_checkpoint
        next_seq_fn: Callable[[], int], # Session._next_seq reader
        append_history_fn: Callable,    # Session._append_history
        chat_scheme_name: "str | None" = None,  # #1593 PR-2: chat-layer ToolUseScheme name → RouterLoop(scheme_name=); None → universal default
        empty_stop_retry: bool = False,  # #4677: chat.empty_stop_retry (owner default False, 2026-08-14) — was hardcoded True below
        _loop_observer: "Callable | None" = None,  # Tier-2 test seam: called with the constructed RouterLoop before run
    ) -> None:
        self._router_host = router_host
        self._safety = safety
        self._router_max_iterations = router_max_iterations
        self._budget_tracker = budget_tracker
        self._non_interactive = non_interactive
        self._exclude_tools = exclude_tools
        self._excluded_categories = excluded_categories  # #1667
        self._contextual_permission = contextual_permission  # #1827 S3
        self._contextual_for_turn_fn = contextual_for_turn_fn  # #1827 S4b
        self._budget = budget
        self._resolver = resolver
        self._compaction = compaction
        self._compaction_controller = compaction_controller
        self._token_learner = token_learner
        self._events = events
        self._model_override_fn = model_override_fn
        self._loop_observer = _loop_observer
        self._history_buffer = history_buffer
        self._budget_advisor = budget_advisor
        self._limit_checkpoint_fn = limit_checkpoint_fn
        self._next_seq_fn = next_seq_fn
        self._append_history_fn = append_history_fn
        self._chat_scheme_name = chat_scheme_name  # #1593 PR-2
        self._empty_stop_retry = empty_stop_retry  # #4677
        # #1468: per-turn cooperative cancellation flag + asyncio.Event for
        # deep-cancel propagation into running subprocess ops (#1470).
        self._turn_cancel_requested: bool = False
        self._turn_cancel_event: asyncio.Event = asyncio.Event()
        # #5618: the recovery-episode state the TUI's shrink-progress row
        # gates on. NOT the controller's `_compacting` flag — that one rises
        # only inside `force_compact_now`, and the retry ladder below calls
        # the engine directly (`_run_with_shrink`), so a real overflow
        # recovery never lifted it and the row structurally never appeared
        # (owner, real machine, mid-recovery: "tui では進捗わからない").
        # Depth, not a bool, because the ladder re-enters: the inner segment
        # and the outer `except _UnrecoveredError` (reactive spill →
        # force_compact_now → resend) BOTH scope an episode, and a bool
        # would end the episode at the inner exit while recovery is still
        # running — the row would blink off mid-way. The number is what
        # `Session.compaction_progress_raw` joins the cached figures against
        # for freshness, so it must advance only on a genuinely NEW recovery,
        # never on another segment of the current one — see
        # `_recovery_episode_scope`'s `continues_previous` for the case where
        # the two segments do not nest and the rule needs saying out loud.
        self._recovery_depth: int = 0
        self._recovery_episode_number: int = 0
        # 0062: per-session structured-output override, set post-construction by
        # ``configure_structured_output`` (mirrors the existing ``_loop_observer``
        # Tier-2 test-seam pattern — a public post-construction configure call, NOT
        # a constructor kwarg thread-through every Session caller would need to
        # touch). None for every Session except an ephemeral agent-step spawn
        # whose ``AgentStep.schema`` is set — byte-identical otherwise.
        self._response_format: "dict | None" = None
        self._schema_validate_fn: "Any | None" = None
        self._max_schema_reprompt_attempts: int = 2
        # #1470: wire cancel_event onto router_host so make_router_op_context
        # can thread it into OpContext → sandboxed_exec backend.
        _set_fn = getattr(router_host, "_set_cancel_event", None)
        if callable(_set_fn):
            _set_fn(self._turn_cancel_event)

    # ── Recovery episode (#5618) ─────────────────────────────────────────────

    @property
    def recovery_episode(self) -> "int | None":
        """#5618: the current overflow-recovery episode number, or None when
        this driver is not recovering. A STATE the consumer reads (the TUI
        polls a snapshot on its own heartbeat), never an edge it has to be
        present to catch — an event-arrival inference cannot answer "is it
        still going?" at an arbitrary later read, which is the only question
        the progress row actually asks.

        Cheap (two int reads), safe every render frame.
        """
        return self._recovery_episode_number if self._recovery_depth > 0 else None

    @contextlib.contextmanager
    def _recovery_episode_scope(self, *, continues_previous: bool = False):
        """#5618: enter/exit one recovery-episode segment, re-entrantly.

        The `finally` is the load-bearing part: an exception, or a cancel,
        must still leave the depth at 0 — this regulates RESIDUE, not
        ordering, so no call site needs to know about any other.

        ``continues_previous`` exists because the two scopes do NOT nest. The
        outer one is opened from an `except` clause, i.e. AFTER the inner one
        has already unwound, so a plain 0→1 transition there would allocate a
        SECOND number for what the user experiences as one uninterrupted
        recovery — and the figures stamped during the first half would then be
        judged stale and blank out mid-recovery, which is the flicker this
        design exists to avoid. Passing it says "this is the same recovery,
        keep its number". The caller only passes it when the number it is
        adopting was allocated during its OWN call, so a stale number from an
        earlier recovery can never be resumed.
        """
        self._recovery_depth += 1
        if self._recovery_depth == 1 and not continues_previous:
            self._recovery_episode_number += 1
        try:
            yield
        finally:
            self._recovery_depth -= 1

    # ── Cancel lifecycle (#1468 / #1470) ─────────────────────────────────────

    @property
    def cancel_event(self) -> asyncio.Event:
        """Per-turn asyncio.Event set by request_cancel(), cleared at turn entry."""
        return self._turn_cancel_event

    def is_cancel_requested(self) -> bool:
        """True when a cooperative turn cancel has been requested.

        Polled at the top of each run_loop iteration via
        RouterHostAdapter.turn_cancel_fn. The flag is reset at turn entry
        so idle cancel calls (Ctrl-C while no turn is running) are
        spurious-safe.
        """
        return self._turn_cancel_requested

    def request_cancel(self) -> None:
        """Set the cooperative cancel flag and cancel_event. Called by cancel_inflight()."""
        self._turn_cancel_requested = True
        self._turn_cancel_event.set()

    # ── Structured output (0062) ─────────────────────────────────────────────

    def configure_structured_output(
        self, *,
        response_format: "dict | None",
        schema_validate_fn: "Any | None" = None,
        max_reprompt_attempts: int = 2,
    ) -> None:
        """Configure the NEXT ``run_turn`` call's answer turn to be a
        schema-constrained ``response_format`` call (0062 §2.1) instead of
        emitting the tool-turn's own free-form text. Called by
        ``session_api.run_agent_step`` right after spawning a
        ``schema``-bearing agent-step's ephemeral session, before its one
        ``MessageBus.request`` turn — mirrors the existing ``_loop_observer``
        Tier-2 seam's "configure the constructed session before its turn"
        shape, but is production wiring (not test-only): every OTHER Session
        never calls this, so ``RouterLoop(response_format=None, ...)`` (this
        driver's default) keeps every other turn byte-identical."""
        self._response_format = response_format
        self._schema_validate_fn = schema_validate_fn
        self._max_schema_reprompt_attempts = max(0, int(max_reprompt_attempts))

    # ── Model resolution ──────────────────────────────────────────────────────

    def _effective_router_model_class(self) -> str:
        """Return the model class to use for router LLM calls this turn.

        /model override wins when set; otherwise falls back to the router
        purpose-class (class_for_purpose("router"), which honours the
        operator's model_class_by_purpose config — byte-identical to the
        pre-/model behaviour when no override is active).
        """
        override = self._model_override_fn()
        if override:
            return override
        from reyn.llm.model_resolver import resolve_purpose_class
        return resolve_purpose_class(None, self._resolver, "router")

    # ── Cap enforcement ───────────────────────────────────────────────────────

    async def _check_cap(self, user_text: str) -> None:
        """Increment the per-turn router invocation counter and enforce the cap.

        Raises RouterCapExceeded when the counter would exceed the configured
        cap. cap=0 disables the check.

        FP-0005: when ``safety.on_limit.mode`` is ``interactive`` /
        ``auto_extend`` and the cap is hit, ask the user / auto-extend
        before re-raising. On approval the cap is extended by the configured
        amount and the run continues.
        """
        from reyn.runtime.errors import RouterCapExceeded
        try:
            self._budget.check_and_increment_router_cap(user_text)
        except RouterCapExceeded as exc:
            decision = await self._limit_checkpoint_fn(
                kind="router_cap",
                prompt=(
                    f"Router hit the per-turn cap of {exc.cap} invocations. "
                    f"Allow more invocations this turn?"
                ),
                detail=(
                    f"count={exc.count} cap={exc.cap} "
                    f"last_reason={exc.last_reason}"
                ),
                extension_amount=1.0,
            )
            if not decision.allow_continue:
                raise
            # Approved — extend the cap and increment for THIS attempt.
            self._budget.extend_router_cap(int(decision.extension))
            self._budget.check_and_increment_router_cap(user_text)

    # ── Overflow-resilient router invocation ─────────────────────────────────

    @staticmethod
    def _spill_candidates(
        head: "list[dict]", raw_middle: "list[dict]", tail: "list[dict]",
    ) -> "list[dict]":
        """#5364 (owner ruling, verbatim "mid も対象にしてね。
        head->mid->tail->open"): spill candidates in STAGED order —
        ``head`` entirely before ``raw_middle`` entirely before ``tail``
        (open-turn is handled by the caller, outside this function) —
        largest-first WITHIN each stage.

        ``raw_middle`` (mid) is included even though spilling it moves
        ZERO wire bytes (conclusion unchanged by #5367's removal of
        ``build_history``'s own elide branch — the real reason was always
        structural, not "elided out": ``estimate_wire_bytes`` simply never
        takes ``raw_middle`` as an argument at all, only ``summary`` —
        ``build_history`` now returns every candidate turn raw regardless,
        but that has no bearing on what THIS function measures):
        #5364 §1.3 names two byte-independent reasons to spill it
        anyway — (1) ``spilled`` is PERSISTENT (D), so a mid turn that
        later slides into head/tail (as the window advances) is already
        done; (2) when overflow later folds ``raw_middle`` into a summary,
        the fold reads the (now-smaller) ref instead of the full body,
        shrinking compaction's own input. The caller must not treat "this
        candidate moved no bytes" as failure for a mid-stage candidate —
        see ``_attempt_reactive_spill``.

        #5514 §7-1 (owner ruling, removes the OLD ``role == "tool"``
        eligibility restriction): ANY turn with an inline string body is
        a candidate now — eligibility is decided by
        ``ChatMessage.spillability`` (persisted on ``meta`` — #5514 §2),
        never by ``role``, which cannot express this axis at all (a
        cross-agent injection carries no literal role; reyn's own FRAME
        and MATERIAL notices share the same role — see
        ``Spillability``'s own module docstring). ``spillability ==
        NEVER`` turns are EXCLUDED entirely, not merely deprioritised —
        losing them would falsify the model's own world-state (#5514
        §1.1). An already-ref'd/non-string content still has nothing
        left to spill regardless of its declared spillability.

        ``new_msg`` (the turn's own newest user message) is never passed
        in here at all — contract's own "user自身の最新messageは落とさ
        ない" is satisfied structurally, not by a filter that could miss
        it.

        #5514 §3/§7-2: within ``head``/``tail`` — wire content the model
        can read back directly — ``spillability`` plays NO part; only
        size orders them (unchanged from before this PR). ``mid``
        (``raw_middle``) is different: it is ``compact()``'s OWN input,
        so which turn goes first there decides what the SUMMARY is built
        from, not what the model reads back — so mid orders by tier
        FIRST (``FIRST_CHOICE`` before ``LAST_RESORT``, size-descending
        within each tier), stage second. #5531 §9.5 (owner, no cursor):
        this ordering is recomputed FRESH from whatever candidates
        remain each time it is called — never persisted state that could
        go stale, the exact `_compact_attempt_len`-shaped bug class this
        arc's own review is watching for.

        "Spilled candidates first" was considered and rejected (owner):
        once spilled, a candidate's only remaining state is ``lost`` either
        way, so an already-spilled entry does not need protecting from
        being resorted alongside an unspilled one — E and C (the size-cap
        GC) serve different purposes (E = minimize round-trips and
        collateral spilling this turn; C = evict low-value data over time).
        """
        def _eligible(turns: "list[dict]") -> "list[dict]":
            # A summary element (`wrap_summary_as_message`) never carries a
            # `spillability` key — reserved/NEVER by construction (#5514
            # §4's SP/new_msg/summary closing note). Its absence must not
            # read as "eligible by default"; excluded by role explicitly,
            # matching `_spill_fn`'s own same-PR guard.
            return [
                t for t in turns
                if isinstance(t.get("content"), str)
                and t.get("role") != SUMMARY_MESSAGE_ROLE
                and t.get("spillability") != Spillability.NEVER.value
            ]

        def _by_size_desc(turns: "list[dict]") -> "list[dict]":
            return sorted(turns, key=lambda t: -len(t["content"]))

        _mid = _eligible(raw_middle)
        _mid_ordered = (
            _by_size_desc([
                t for t in _mid if t.get("spillability") == Spillability.FIRST_CHOICE.value
            ])
            + _by_size_desc([
                t for t in _mid
                if t.get("spillability") not in (
                    Spillability.FIRST_CHOICE.value, Spillability.NEVER.value,
                )
            ])
        )
        return (
            _by_size_desc(_eligible(head))
            + _mid_ordered
            + _by_size_desc(_eligible(tail))
        )

    def _spill_batch_within_face(
        self, turns: "list[dict]", *, chain_id: str, granularity: str,
        seq_fn: "Callable[[int, dict], int]",
    ) -> "list[tuple[int, dict]]":
        """#5592 (owner ruling, "1 request = 面 × 同一 Spillability";
        "head と tail を混ぜない") — within ONE face (a raw_middle slice,
        ``head``, or ``tail`` — the caller never mixes faces into one
        call), try the highest-priority non-empty ``Spillability`` tier's
        eligible candidates (FIRST_CHOICE, else LAST_RESORT; ``NEVER``
        excluded — same ``_eligible``/``_by_size_desc`` shape
        ``_spill_candidates`` already established), largest-first,
        actually spilling (persists via ``spill_turn_content``) up to K
        of them.

        K is the ONE thing ``spill_granularity`` controls — unlimited
        (the whole tier) for ``"tier"`` (the default: #5592's own accept
        — an N-candidate tier costs 1 request, not N), or ``1`` for
        ``"turn"`` (the pre-#5592 behavior, reproduced byte-for-byte).
        One algorithm, one step size — not two code paths (architect's
        own "if で2経路を作らないでください": two paths get tested and
        maintained separately, and only one of them stays exercised in
        practice).

        If a tier's own candidates all turn out to be no-ops (already
        spilled, or ``spill_turn_content`` itself returns the input
        unchanged), falls through to the NEXT tier — matching the pre-
        #5592 single-candidate search's own behavior of skipping a
        non-progressing candidate rather than stopping there. A tier with
        zero eligible candidates is skipped without ever calling
        ``spill_turn_content`` — #5592 owner ruling: "母数が0なら request
        投げる意味あると思ってるの？" (never spend a request on an empty
        population; the mid-face caller's own retry_loop only re-invokes
        ``compact()`` when this returns a non-empty batch, so an empty
        tier here costs nothing).

        Returns (index-into-``turns``, replacement-turn) pairs actually
        applied — never mutates ``turns`` itself. ``seq_fn`` lets each
        caller keep its own pre-#5592 ``seq=`` convention for
        ``spill_turn_content`` (mid: the turn's own ``seq`` field, falling
        back to 1; head/tail: the candidate's own position) rather than
        this shared method inventing a third one."""
        def _eligible(
            indexed: "list[tuple[int, dict]]",
        ) -> "list[tuple[int, dict]]":
            return [
                (i, t) for i, t in indexed
                if isinstance(t.get("content"), str)
                and t.get("role") != SUMMARY_MESSAGE_ROLE
                and t.get("spillability") != Spillability.NEVER.value
            ]

        def _by_size_desc(
            indexed: "list[tuple[int, dict]]",
        ) -> "list[tuple[int, dict]]":
            return sorted(indexed, key=lambda it: -len(it[1]["content"]))

        _elig = _eligible(list(enumerate(turns)))
        _tiers = (
            _by_size_desc([
                it for it in _elig
                if it[1].get("spillability") == Spillability.FIRST_CHOICE.value
            ]),
            _by_size_desc([
                it for it in _elig
                if it[1].get("spillability") != Spillability.FIRST_CHOICE.value
            ]),
        )
        _k = None if granularity == "tier" else 1
        for _tier_members in _tiers:
            if not _tier_members:
                continue
            _edits: "list[tuple[int, dict]]" = []
            for idx, turn in _tier_members:
                if self._history_buffer.is_already_spilled(turn["content"]):
                    continue
                replacement = self._history_buffer.spill_turn_content(
                    turn["content"], chain_id=chain_id,
                    # #5564: name this write by the turn's own origin —
                    # never the bare "tool" default for a non-tool
                    # candidate.
                    tool=turn.get("name") or turn.get("role") or "history",
                    seq=seq_fn(idx, turn),
                )
                if replacement is None or replacement == turn["content"]:
                    continue
                _edits.append((idx, {**turn, "content": replacement}))
                if _k is not None and len(_edits) >= _k:
                    break
            if _edits:
                return _edits
        return []

    async def _attempt_reactive_spill(self, *, chain_id: str) -> bool:
        """#5296 PR-2 ②③ / #5364 §1.6 (停止条件と束縛), #5592 (batching):
        spill a BATCH from the first face (head → mid → tail, staged,
        NEVER mixed into one call — owner ruling "head と tail を
        まとめない") that yields genuinely NEW offloads, then stop — the
        caller retries the actual LLM call once after this returns
        ``True``, rather than this method trying to decide on its own
        whether enough has been spilled.

        #5592 (owner ruling, "1 request = 面 × 同一 Spillability", "main_call
        も含むに決まってる"): each face is tried via
        ``_spill_batch_within_face`` — the whole highest-priority
        eligible ``Spillability`` tier within that ONE face, in a single
        batch (``chat.compaction.spill_granularity`` controls the batch
        size; see that method's own docstring), rather than #5364's
        original one-candidate-at-a-time search across all three faces
        merged. Real-machine motivation (#5592): a persistent overflow
        recovering one candidate per ``main_call`` retry is O(candidate
        count) LLM calls; per-face-per-tier batching is O(face count ×
        tier count) = a small constant, independent of history length.

        Returns ``True`` (progress — at least one candidate in the tried
        face's own highest-priority non-empty tier was newly spilled)
        if a batch was applied this call; ``False`` (failure — every
        face's every tier contributed nothing new) if none was.

        #5364 §1.6 (owner/architect ruling, replacing the OLD "undo if it
        didn't help" behavior): a genuine new spill is ALWAYS kept, never
        undone — not by a byte-decrease check (a ``raw_middle`` candidate
        can NEVER move wire bytes at all — ``estimate_wire_bytes`` simply
        never takes ``raw_middle`` as an input at all, structural and
        unaffected by #5367's removal of ``build_history``'s own elide
        branch — see ``_spill_candidates``'s own docstring above for the
        corrected reasoning — #5364 §1.3), and not by a "made it bigger"
        check either (a tiny original body
        can genuinely become a larger fixed-overhead offloaded-preview
        replacement, and that is still real, durable progress: ``spilled``
        is persistent (D), and a mid-turn spill still shrinks a LATER
        compaction fold's own input even when it moves zero bytes today).
        The stop condition is "did progress happen" (at least one
        candidate got consumed), never "did bytes move" — reading wire
        bytes at all would reintroduce exactly the confusion #5367③
        already named for ``retry_loop``'s own analogous floor (mistaking
        "this lever alone didn't visibly help" for "no lever is left").

        No longer takes ``user_text`` (#5296 PR-2's original signature
        did — used only to estimate wire bytes, which this method no
        longer does at all).
        """
        head, raw_middle, tail, _summary, _ = (
            self._history_buffer.decompose_history_for_retry()
        )
        _granularity = self._compaction.spill_granularity
        # #5364 §1.3 (staged order, unchanged): head entirely before mid
        # entirely before tail entirely — never interleaved, never mixed
        # into one ``_spill_batch_within_face`` call (that is what "head
        # と tail をまとめない" means at THIS granularity: a batch is
        # scoped to one face's one tier, never spanning two faces).
        for _face in (head, raw_middle, tail):
            _edits = self._spill_batch_within_face(
                _face, chain_id=chain_id, granularity=_granularity,
                # Pre-#5592 head/tail/mid-via-this-path convention: the
                # candidate's own position within its face (unchanged —
                # the old merged-list ``i + 1`` degenerates to this
                # exact value for a single-face, single-candidate call,
                # since a batch here never spans more than one face).
                seq_fn=lambda idx, _turn: idx + 1,
            )
            if _edits:
                return True
        # Every face's every tier contributed nothing new — candidates
        # are exhausted (#5364 §1.6's failure predicate).
        return False

    async def _run_with_shrink_and_byte_reduction(
        self, loop: Any, user_text: str, *, chain_id: str,
    ) -> Any:
        """#5296 PR-2 / #5364 §1.6 wrapper: same-turn recovery for an
        ``UnrecoveredError`` — MODE-INDEPENDENT (byte-limited HTTP 413 OR
        a non-byte, token-axis terminal cause — #5364 §1.6, replacing the
        OLD ``if not exc.saw_byte_limit: raise`` gate, which meant a
        token-cause overflow never got a single spill attempt at all,
        purely because of which axis happened to terminate it).
        ``ContextOverflowError`` (the window itself is too small — no
        history to shrink) is UNCHANGED, not caught here.

        ``_run_with_shrink``'s own contract is UNCHANGED (architect
        ruling) — this wrapper only decides whether to call it AGAIN after
        it raises, never alters what it does internally.

        #5364 §1.6's predicates (own body is the canonical source) —
        TWO reduction axes, each with its OWN progress signal (architect
        review: the first version of this fix read only the spill axis,
        contradicting #5367's own "縮小軸は2本／失敗＝両縮小軸が dry" —
        that omission was this docstring's, not the implementation's; the
        implementation was faithful to a §1.6 text that only named the
        spill side):

        - spill axis PROGRESS — the un-spilled candidate count went down
          by one (:meth:`_attempt_reactive_spill`'s own return value).
        - compact axis PROGRESS — the compaction watermark advanced
          (``self._history_buffer.compaction_watermark()`` strictly
          increased across this attempt) — a STRUCTURAL fact, never a
          byte count (mid spills move zero wire bytes by construction,
          #5364 §1.3, so bytes cannot measure EITHER axis). This is
          ``_run_with_shrink``'s own PRE-EXISTING #4954(b) ``next_turn``
          side-effect (``force_compact_now``, inside ITS except block,
          called before the exception ever reaches here) — not a new
          call this wrapper makes.
        - SUCCESS — ``_run_with_shrink`` stops raising (realized directly
          by the ``try`` below returning; no separate byte-target
          comparison is needed — the ACTUAL call succeeding IS success,
          a stronger signal than any estimate of it. Cost disclosure: one
          ``acompletion`` per spill — an N-candidate turn sends the full
          payload N times; N is almost always 1 in practice, and batching
          "spill everything, then retry once" is a separate, un-filed
          improvement, not this PR's scope).
        - FAILURE — NEITHER axis progressed this attempt; re-raise.
          Termination holds: the watermark is bounded above (total seq
          count) and monotonically non-decreasing, the candidate count is
          bounded below by 0 and monotonically non-increasing, and every
          retried iteration must strictly advance at least one of the
          two — so this loop provably ends within at most
          (remaining candidates + remaining foldable seqs) iterations.
          ``force_compact_now`` itself cannot loop forever either (its
          own docstring: returns immediately when already compacting, or
          when it finds zero candidates).

        This REMOVES the OLD fixed ``_MAX_BYTE_REDUCTION_ATTEMPTS`` cap
        entirely (§1.6: "定数は廃止") and the OLD
        ``self._wire_bytes_now() >= attempt_start_bytes: raise`` check —
        that check treated "this spill didn't move wire bytes" as failure,
        which is exactly wrong for a ``raw_middle``-only spill (#5364
        §1.3) — with candidates still available, that old check would
        raise on the FIRST mid-only spill instead of trying the next one.

        force_compact_now double-call check (asked for explicitly, #5364
        §1.6; corrected #5578): ``_run_with_shrink``'s OWN except block
        conditionally calls ``force_compact_now()`` — pre-#5578, ONLY when
        ``saw_byte_limit`` was True; #5578 dropped that axis gate, so it
        now fires whenever ``fold_persist_policy == "next_turn"``, on EITHER
        axis (see that except block's own comment for why the old
        byte-only gate's reason — #4885's proactive pre-trigger already
        handling the token axis — no longer holds; #5528 removed that
        pre-trigger). A token-cause retry CAN now re-enter
        ``_run_with_shrink`` and hit that side-effect again on this
        wrapper's own next iteration — this is NOT a new double-call risk:
        ``force_compact_now()`` itself already returns immediately once
        the watermark has caught up to everything durably available (zero
        candidates), the SAME bound the byte-axis case already relied on
        before this fix. Each repeat is a cheap no-op, not a repeated
        summarization LLM call.
        """
        from reyn.services.compaction.engine import UnrecoveredError as _UnrecoveredError

        attempt = 0
        # #5618: the OUTER episode scope. It must outlive the `except` block
        # that opens it — the recovery continues through the reactive spill,
        # the force_compact, AND the resend on the next lap, and the user is
        # waiting on all of it as one wait. A plain `with` inside the except
        # would close at `continue` and blink the progress row off between
        # laps, so the scope is parked on an ExitStack that unwinds when this
        # wrapper leaves, however it leaves (return, re-raise, or cancel).
        # Entered at most ONCE (the stack is only non-empty after the first
        # failure): the resend's own inner scope then nests at depth 2 and
        # does not bump the episode number, so one recovery keeps one number
        # and the figures cached under it stay fresh across every lap.
        episode_entered = False
        # #5618: the number the ladder below allocated for THIS call, if it
        # got that far. `_run_with_shrink` opens its own episode on the
        # overflow path, so by the time an UnrecoveredError reaches us the
        # number has usually already moved — and the outer scope should then
        # adopt it rather than allocate a fresh one (see
        # `_recovery_episode_scope`). Comparing against the value captured
        # HERE is what keeps that adoption honest: if the ladder never opened
        # an episode this call, the number is some earlier recovery's and must
        # not be resumed.
        episode_at_entry = self._recovery_episode_number
        with contextlib.ExitStack() as episode_stack:
            while True:
                watermark_before = self._history_buffer.compaction_watermark()
                try:
                    return await self._run_with_shrink(loop, user_text, chain_id=chain_id)
                except _UnrecoveredError:
                    if not episode_entered:
                        episode_stack.enter_context(self._recovery_episode_scope(
                            continues_previous=(
                                self._recovery_episode_number != episode_at_entry
                            ),
                        ))
                        episode_entered = True
                    # Compact axis: measured FIRST — `_run_with_shrink`'s own
                    # except block (its PRE-EXISTING #4954(b) side-effect) may
                    # already have advanced the watermark during the call
                    # above, before this exception ever reached here.
                    compact_progressed = (
                        self._history_buffer.compaction_watermark() > watermark_before
                    )
                    # Spill axis: always attempted too, even if compact already
                    # progressed — a genuinely spillable candidate should still
                    # be spilled (§1.6's own "1つずつ spill" contract), not
                    # skipped just because the OTHER axis happened to move.
                    spill_progressed = await self._attempt_reactive_spill(chain_id=chain_id)
                    if not compact_progressed and not spill_progressed:
                        raise
                    attempt += 1
                # #5296 PR-2 ⑥: chain_id is the SAME value this call already
                # closes over — no new chain_id, no re-emitted
                # user_submitted/turn_started/WAL append (those all live
                # ABOVE run_turn, in Session — unreached by looping back to
                # the top of this while-loop and calling _run_with_shrink
                # again from HERE).
                self._events.emit(
                    "payload_reduced", chain_id=chain_id, attempt=attempt,
                )
                continue

    async def _run_with_shrink(self, loop: Any, user_text: str, *, chain_id: str = "") -> Any:
        """Run the router once with the reactive bounded-shrink ``retry_loop``.

        Returns the router usage, or raises ``_ContextOverflowError``
        (the context window is genuinely too small) or ``_UnrecoveredError``
        (shrinking recovered the SAME cause repeatedly without resolving
        it — #4885: e.g. an HTTP 413 request-body-byte limit, a DIFFERENT
        axis from token count, which shrinking cannot address) when even
        the floor overflows — the caller (#4381 PR-4) treats either as
        unrecovered on the first attempt; there is no consolidating retry
        loop here anymore (#1092 PR-F2b's force-close handoff, removed).
        Still rebuilds the history each call — a plain re-entry after ANY
        earlier turn's edits (compaction, etc.), not specifically a
        force-close one.

        No ``router_context_overflow_unrecovered`` event here — the caller
        is the one that emits it, on the overflow it treats as terminal.

        #5256: a provider usage-window/plan quota exhaustion is checked
        FIRST and re-raised UNWRAPPED without entering the shrink loop at
        all — shrinking cannot fix a clock-based limit, and trying anyway
        (compaction's own LLM call) spends more of the same exhausted
        quota. This is NOT one of the two overflow outcomes above; the
        caller's own except clause does not catch it, so it reaches
        Session's generic exception handler, which keeps the session
        alive (owner ruling).

        ``chain_id`` (#5367③, default ``""`` when not from the one real
        caller below): threaded through to ``retry_loop``'s injected
        ``spill_fn`` so a same-turn content-level spill (at either
        terminal floor retry_loop can hit) writes through the SAME
        ``MediaStore.save_tool_result`` metadata shape
        ``RouterHistoryBuffer.spill_turn_content``'s other caller
        (``_attempt_reactive_spill``) already uses — no new store-path
        convention.
        """
        # #5577/#5593: both call sites (the one below and the one in
        # ``_router_main_call_for_retry``, extracted out of this method by
        # #5631) now classify through ``_is_shrinkable_overflow`` — see that
        # helper's own docstring and each call site's own comment for what
        # changed and why.
        # #4954(b): `UnrecoveredError` IS caught below, but only to trigger a
        # real compaction as a side effect — it is always re-raised unchanged,
        # so it still propagates unwrapped to `run_turn`'s own widened except
        # exactly as #4885 established. Not a reversion of that fix. (The
        # `.saw_byte_limit` read this comment used to describe is gone: #5578
        # made the trigger axis-agnostic.)

        # #4995: dispatched to a worker thread, not run inline — build_history()
        # re-serialises every watermark-surviving turn on every call, a cost
        # proportional to session length, and it would otherwise run
        # synchronously on the SAME event loop the TUI's frame scheduling
        # shares. No ownership guard is threaded through any more: #5367
        # retired the elide cache this used to protect, so `build_history()`
        # mutates no shared cross-call state for a stale write to corrupt.
        history = await asyncio.to_thread(self._history_buffer.build_history)
        try:
            return await loop.run(user_text=user_text, history=history)
        except Exception as _exc:
            # Classified through ``_is_shrinkable_overflow`` — see that
            # helper's own docstring for why neither `classify_llm_failure`
            # alone nor a keyword match is sufficient (#5577, #5593).
            #
            # Re-raising when NOT a shrinkable overflow (never wrapped in
            # ContextOverflowError/UnrecoveredError) means it propagates to
            # run_turn's own except, then to Session._handle_inbox_text's
            # generic catch-all — which already does the right thing for
            # an un-wrapped exception: surface it via the outbox
            # (classify_router_error) and return normally, keeping the
            # session alive (owner ruling, #5256: quota exhaustion must
            # never end the session — unaffected by this change, see
            # test_5256_quota_not_context_overflow.py).
            if not _is_shrinkable_overflow(_exc):
                raise
            self._events.emit(
                "router_context_overflow_detected", error=repr(_exc)
            )
            # The summary element is unused here: it already sits inside
            # `_head`/`_tail` themselves (#5531 PR-2).
            # `seq_by_id` (id(wire_dict) -> real seq) is the ONLY way to
            # derive a real `covers_through_seq` for whatever retry_loop's own
            # fold produces — that ChatSummary's own field is structurally 0 on
            # this path, because wire dicts carry no `seq` (#5498/#5578). See
            # `_persist_recovery_fold`, which consumes it.
            _head, _raw_middle, _tail, _, _seq_by_id = (
                self._history_buffer.decompose_history_for_retry()
            )
            _new_msg = {"role": "user", "content": user_text}

            return await self._drive_retry_ladder(
                _RetryPayload(
                    head=_head, raw_middle=_raw_middle, tail=_tail,
                    new_msg=_new_msg, seq_by_id=_seq_by_id,
                ),
                loop=loop, user_text=user_text, chain_id=chain_id,
            )

    async def _drive_retry_ladder(
        self, payload: "_RetryPayload", *, loop: Any, user_text: str,
        chain_id: str,
    ) -> Any:
        """Run the bounded-shrink ladder for one recovery episode.

        Extracted from ``_run_with_shrink`` (#5631). It takes a
        ``_RetryPayload`` rather than the five decomposition outputs
        separately: passing them flat needs 8 parameters, past the 6 the
        issue sets as the point where a Parameter Object is warranted, and
        they are not five independent arguments — they are one function's
        single output, which is what makes the object honest here rather
        than a bag assembled to shorten a signature.
        """
        # Nothing here decides whether or where a summary appears: one
        # arrives already decorated wherever it naturally sits in
        # `head`/`tail` (#5531 PR-2).
        #
        # `_ContextOverflowError` (window too small) and
        # `_UnrecoveredError` (shrinking cannot fix this cause) each
        # propagate AS THEMSELVES — merging them renamed the second's
        # correct diagnosis into the first's wrong one, which is the
        # defect #4885 fixed. `run_turn`'s own except catches both.
        #
        # #5592: retry_loop is the ONE production call site for the
        # upstream-call counter — started here and reset in `finally`
        # however retry_loop exits, so a stale counter never leaks into a
        # later, unrelated LLM call.
        from reyn.llm.llm import (
            reset_upstream_recovery_call_counter as _reset_upstream_recovery_call_counter,
        )
        from reyn.llm.llm import (
            start_upstream_recovery_call_counter as _start_upstream_recovery_call_counter,
        )
        from reyn.services.compaction.engine import (
            UnrecoveredError as _UnrecoveredError,
        )
        from reyn.services.compaction.engine import retry_loop as _retry_loop
        # #5618: this segment IS the ladder (rungs ①〜④) — entering the
        # episode here is what finally lifts the progress row's gate on
        # the path that actually runs during a real overflow recovery.
        # Deliberately the same region the #5592 upstream-call counter
        # already scopes: the numbers the row displays come from that
        # counter, so the episode the row is gated on and the episode
        # those numbers belong to are the same span by construction,
        # not by two independently-maintained boundaries agreeing.
        with self._recovery_episode_scope():
            _upstream_counter_token = _start_upstream_recovery_call_counter()
            try:
                try:
                    _shim = await _retry_loop(
                        SP=self._history_buffer.build_system_prompt(),
                        head=payload.head,
                        raw_middle=payload.raw_middle,
                        tail=payload.tail,
                        new_msg=payload.new_msg,
                        cfg=self._compaction,
                        model=self._effective_router_model_class(),
                        engine=self._compaction_controller._engine,
                        learner=self._token_learner,
                        main_call=_partial(
                            self._router_main_call_for_retry,
                            loop=loop, user_text=user_text,
                        ),
                        spill_fn=_partial(
                            self._spill_batch_for_retry, chain_id=chain_id,
                        ),
                        on_summary_used=_partial(
                            self._persist_recovery_fold, seq_by_id=payload.seq_by_id,
                        ),
                        # #5531 §10: no `max_iterations=` any more —
                        # retry_loop abolished its iteration-count bound
                        # (see its own "Bounded termination proof"
                        # docstring). #4957's `chat.compaction.
                        # max_shrink_iterations` config knob is therefore
                        # ORPHANED by this change (nothing reads it any
                        # more) — disclosed, not silently left: removing
                        # the knob itself (schema/validation/docs/the ~10
                        # test fixtures that still pass it) is its own
                        # scoped follow-up, not folded into this already-
                        # large PR.
                    )
                finally:
                    _reset_upstream_recovery_call_counter(_upstream_counter_token)
            except _UnrecoveredError:
                # #4954 (b), architect-ruled, WIDENED #5578: on ANY
                # UnrecoveredError exhaustion (byte-limit 413 OR a
                # non-byte, token-axis terminal cause — no longer gated on
                # `_unrecovered.saw_byte_limit`), trigger a REAL compaction
                # here — in the driver's except block, not inside
                # retry_loop itself (retry_loop stays a pure TRANSPORT
                # operation; compaction is the SEMANTIC operation that
                # actually retires history entries, Session's own
                # docstring: "the only operation meant to retire an
                # entry"). Routed through `force_compact_now` — the SAME
                # durable-watermark path `ContextBudgetAdvisor`'s
                # pre-frame guard already uses
                # (`_durable_active_history_after`-backed, continuous from
                # the last real `covers_through_seq`) — deliberately NOT
                # `retry_loop`'s own compaction result: its `covers` can
                # cover only `raw_middle` while skipping `head` entirely,
                # which is not continuous from the previous watermark and
                # would silently mark the OLDEST unsummarized part of
                # history "covered" without ever summarizing it (exactly
                # owner's own real-machine shape).
                #
                # Axis-agnostic since #5578 (was gated on
                # `saw_byte_limit`, so a token-cause exhaustion never
                # reached here and had no durable recovery path at all).
                # `fold_persist_policy`'s own docstring declares a stop-line on
                # the irreversible compaction STEP, never on an axis — the
                # widening matched this call site to that contract rather
                # than deciding something new. Full history and the
                # measurement behind it: #5578, #5528.
                #
                # Repeat-bounding (band question 1 — who stops this if it
                # repeats): this block CAN run more than once per turn
                # (`_run_with_shrink_and_byte_reduction` retries on ANY
                # `UnrecoveredError`, #5364 §1.6 below). `force_compact_now`
                # bounds it: it returns immediately when a pass is already
                # running, or when the watermark has caught up to
                # everything durably available — so the second call onward
                # is a no-op, not a repeated summarization.
                if self._compaction.fold_persist_policy == "next_turn":
                    await self._compaction_controller.force_compact_now()
                raise
        return _shim.usage

    @staticmethod
    def _mid_seq_of(_idx: int, turn: dict) -> int:
        """Pre-#5592 mid convention: the turn's own ``seq``, else 1. A named
        function rather than the lambda this used to be (#5631) — it captures
        nothing, and the closure count is the gate."""
        return turn.get("seq", 1)

    async def _persist_recovery_fold(
        self,
        chat_summary: "ChatSummary",
        folded_turns: "list[dict]",
        *,
        seq_by_id: "dict[int, int]",
    ) -> None:
        """#5578: persist a successful recovery's own fold. Was a closure in
        ``_run_with_shrink``; ``seq_by_id`` is the one value it captured and is
        now passed explicitly (#5631).
        """
        # #5578 (architect ruling) — a successful recovery's own
        # fold is persisted here, NOT via force_compact_now (see
        # CompactionController.persist_recovery_summary's own
        # docstring for the full rationale: the fold already
        # happened, this only records it — no new compaction LLM
        # call, no new irreversible step).
        #
        # Gated the SAME way the pre-existing terminal-failure
        # compaction trigger below is (`fold_persist_policy ==
        # "next_turn"`) — lead-coder's own catch (PR #5610 review):
        # `next_turn` is `config/chat.py`'s own DEFAULT
        # (`fold_persist_policy: Literal["never", "next_turn"] =
        # "next_turn"`), so this is opt-OUT, not opt-in — only a
        # deployment that explicitly writes `fold_persist_policy:
        # never` is exempt. #5578's own owner-facing UX-visible
        # point (users see history replaced by a summary earlier
        # than before) is therefore live on the SHIPPED default,
        # for everyone, not only for someone who opted into a
        # non-default setting. `fold_persist_policy == "never"` (the
        # pre-existing #5498 self-test's own configuration) is the
        # one deployment shape that takes NO new persistence
        # action here — unaffected by this change.
        if self._compaction.fold_persist_policy != "next_turn":
            return
        # #5578: derive the REAL covers_through_seq from the
        # decomposition's own seq_by_id — never trust
        # chat_summary.covers_through_seq off this path (always 0,
        # #5498). Only non-summary turns carry a "new" seq worth
        # counting; a role=="summary" element folded alongside
        # them (it can ride inside raw_middle, #5531) has no seq
        # of its own to contribute here.
        _real_seqs = [
            seq_by_id[id(t)] for t in folded_turns
            if id(t) in seq_by_id and t.get("role") != SUMMARY_MESSAGE_ROLE
        ]
        _covers_through_seq = max(_real_seqs, default=0)
        await self._compaction_controller.persist_recovery_summary(
            chat_summary, covers_through_seq=_covers_through_seq,
        )

    def _spill_batch_for_retry(
        self, candidates: "list[dict]", *, chain_id: str,
    ) -> "list[tuple[int, dict]]":
        """#5531 §10 rung①: the spill batch retry_loop is handed. Was a closure
        in ``_run_with_shrink``; ``chain_id`` is the one value it captured and
        is now passed explicitly (#5631).
        """
        # #5531 §10 rung① / #9.6: injected into retry_loop, not
        # imported by it — matches ``context_budget_advisor.py``'s
        # own ``save_fn``-injection style for ``cap_tool_result_
        # content``. ``candidates`` is the SLICE retry_loop is
        # about to (re-)offer to ``compact()`` this attempt (#5592,
        # correcting #9.6's own "never a slice" claim — see
        # ``_spill_batch_from_offered``'s own docstring, engine.py)
        # — never head/tail, a SEPARATE population this closure
        # never sees (see ``_attempt_reactive_spill`` for that
        # face; retry_loop only calls this when raw_middle is
        # non-empty, which coincides exactly with a compact()-
        # origin overflow).
        #
        # #5592 (owner ruling, "1 request = 面 × 同一
        # Spillability"): returns a WHOLE BATCH now, not one
        # candidate — every eligible member of the highest-
        # priority non-empty tier (FIRST_CHOICE, else LAST_RESORT)
        # that genuinely progresses, for ``spill_granularity ==
        # "tier"`` (the default); exactly one, for ``== "turn"``
        # (the pre-#5592 behavior, unchanged byte-for-byte — see
        # ``_spill_batch_within_face``'s own docstring for why
        # this is ONE algorithm with a step size, not two
        # branches). engine.py stays Spillability-agnostic either
        # way (never imports it) — this method owns all ordering.
        # #9.5's own no-cursor rule: re-scans ``candidates`` fresh
        # on every call — no persisted position.
        _edits = self._spill_batch_within_face(
            candidates, chain_id=chain_id,
            granularity=self._compaction.spill_granularity,
            # Pre-#5592 mid convention: the turn's own ``seq``
            # field, falling back to 1 — unchanged.
            seq_fn=self._mid_seq_of,
        )
        if _edits:
            return _edits
        # #5531 §9.6 (owner acceptance): "candidate 0" alone cannot
        # tell "legitimately all-NEVER" apart from "the population
        # got built wrong" — report the breakdown so a broken
        # construction path can't silently masquerade as a normal
        # exhaustion. Counted from `candidates` directly, so a
        # candidate that fits NEITHER bucket (e.g. missing its
        # `spillability` key, or non-str content) shows up as a
        # gap between the sum and `population` — the exact signal
        # this event exists for.
        self._events.emit(
            "spill_candidate_population_exhausted",
            population=len(candidates),
            first_choice_count=sum(
                1 for t in candidates
                if t.get("spillability") == Spillability.FIRST_CHOICE.value
            ),
            last_resort_count=sum(
                1 for t in candidates
                if t.get("spillability") == Spillability.LAST_RESORT.value
            ),
            never_count=sum(
                1 for t in candidates
                if t.get("spillability") == Spillability.NEVER.value
            ),
        )
        return []

    async def _router_main_call_for_retry(
        self, *, SP, head, tail, new_msg, loop, user_text,
    ):
        """The real router call retry_loop drives. Was a closure in
        ``_run_with_shrink``; ``loop`` and ``user_text`` are the two values it
        captured and are now passed explicitly (#5631). ``SP``/``new_msg`` are
        part of retry_loop's own ``main_call`` contract and stay in the
        signature even though this body does not read them.
        """
        from reyn.runtime.usage_shim import _RouterUsageShim
        from reyn.services.compaction.engine import (
            ContextOverflowError as _ContextOverflowError,
        )
        # This is THE one place `head`+`tail` become `loop.run`'s payload, and
        # two kinds of internal annotation converge here: `spillability`
        # (added by `decompose_history_for_retry` for `_spill_candidates`) and
        # a `role == SUMMARY_MESSAGE_ROLE` element, which arrives either from
        # that same turns filter or from retry_loop's fold-success branch
        # appending a fresh `wrap_summary_as_message` result straight into
        # `head`, bypassing `_serialise_turn`. Neither may reach the wire:
        # "summary" is reyn's own discriminator, and a provider validating
        # role names against a fixed enum 400s the request before inference
        # starts, which no amount of shrinking can recover from (#5514 §7-3,
        # #5598; `wire_role`'s own docstring has the incident).
        #
        # Stripped by an ALLOW-list (`_WIRE_MESSAGE_KEYS`, module level), not
        # a deny-list: a deny-list has to name each internal key, and one
        # already leaked past it (`wrap_summary_as_message` spreads `**summary`
        # in, so `topic_arc`/`decisions`/`pending`/… rode along). An allow-list
        # cannot miss a FUTURE key the same way — a new annotation has to be
        # added there deliberately to reach the wire (#5599). See that
        # constant's own docstring for where each key comes from.
        _msgs = [
            {
                **{k: v for k, v in t.items() if k in _WIRE_MESSAGE_KEYS},
                "role": _wire_role(t.get("role", "")),
            }
            for t in list(head) + list(tail)
        ]
        try:
            _usage = await loop.run(user_text=user_text, history=_msgs)
        except Exception as _call_exc:
            # #5577/#5593: unified onto ``_is_shrinkable_overflow``
            # — was is_context_overflow_error alone (#5577's own
            # pre-fix), with NO exclusion for a FATAL exception
            # (AttributeError/TypeError/KeyError) or a RETRYABLE
            # one (5xx/timeout/quota) whose message text happened
            # to contain an overflow keyword; both used to get
            # wrapped as ContextOverflowError and enter the shrink
            # ladder — burning real LLM calls chasing a cause no
            # amount of shrinking can fix, then reporting the
            # wrong diagnosis (#3783's own owner ruling, unreached
            # here until #5577). A genuine overflow (litellm's
            # typed ContextWindowExceededError, a 413, or an
            # overflow-keyword match — see the helper's own
            # docstring for #5593's own correction: an UNRECOGNIZED
            # exception shape now correctly does NOT enter, unlike
            # classify_llm_failure's own bare fallthrough) is
            # unaffected — still wrapped and still shrinks.
            if _is_shrinkable_overflow(_call_exc):
                raise _ContextOverflowError(str(_call_exc)) from _call_exc
            raise
        return _RouterUsageShim(_usage)


    # ── Main turn entry point ─────────────────────────────────────────────────

    async def run_turn(self, user_text: str, chain_id: str) -> None:
        """Run RouterLoop for one user utterance.

        Enforces the per-turn cap, builds history, and calls RouterLoop.run().
        Does NOT modify history or outbox directly — RouterLoop calls host
        callbacks.

        Raises RouterCapExceeded when the per-turn cap is reached.
        """
        from reyn.runtime.router_loop import EMPTY_STOP_RETRY_DIRECTIVE, RouterLoop
        from reyn.services.compaction.engine import (
            ContextOverflowError as _ContextOverflowError,
        )
        from reyn.services.compaction.engine import (
            UnrecoveredError as _UnrecoveredError,
        )

        # #1468 / #1470: reset cancel flag + event at turn entry so an idle
        # cancel_inflight() call (Ctrl-C while no turn is running) is
        # spurious-safe and does not bleed into the next turn.
        self._turn_cancel_requested = False
        self._turn_cancel_event.clear()
        # FP-0005: now async (consults safety.on_limit on hit).
        await self._check_cap(user_text)
        # #1666: per-turn tool_call count cap (cost-bound) sourced from
        # safety.loop.max_tool_calls_per_turn (default 50). 0 = unlimited.
        _max_tool_calls_per_turn = getattr(
            getattr(self._safety, "loop", None),
            "max_tool_calls_per_turn",
            50,
        )
        loop = RouterLoop(
            host=self._router_host, chain_id=chain_id,
            # /model override wins when set; None → RouterLoop resolves router-purpose-class default.
            router_model=self._model_override_fn(),
            # #1593 PR-2: select the chat-layer tool-use scheme (None → universal).
            scheme_name=self._chat_scheme_name,
            max_iterations=self._router_max_iterations,
            budget=self._budget_tracker,
            # #1440 followup: thread the run-once autonomy flag to the LIVE
            # chat-router SP path (router_loop build_system_prompt).
            non_interactive=self._non_interactive,
            # #187: hide excluded tools from the MAIN agent loop's LLM-visible
            # catalog.
            exclude_tools=self._exclude_tools,
            excluded_categories=self._excluded_categories,  # #1667
            # #1827 S4b: per-turn effective contextual — when untrusted external
            # content is live in context (context-auto), the static narrowing is
            # composed with the minimal _untrusted profile; otherwise the static
            # S3 contextual (byte-identical). The fn is provided by Session.
            contextual_permission=(
                self._contextual_for_turn_fn()
                if self._contextual_for_turn_fn is not None
                else self._contextual_permission
            ),
            # B43-NF-W6-1 / #187: chat router empty-stop retry + uniform
            # "resume" directive. #4677 (owner, 2026-08-14): the auto-retry
            # switch is now config-driven (chat.empty_stop_retry, default
            # False) rather than hardcoded True — see that field's own
            # docstring for the incident + tradeoff. The directive itself
            # is still always threaded (it is inert unless
            # empty_stop_retry_auto is True), so a config-driven re-enable
            # needs no other change here.
            empty_stop_retry_directive=EMPTY_STOP_RETRY_DIRECTIVE,
            empty_stop_retry_auto=self._empty_stop_retry,
            # #1666: per-turn tool_call count cap (cost-bound).
            max_tool_calls_per_turn=_max_tool_calls_per_turn,
            # FP-0005: wire safety.on_limit so max_iterations exhaustion routes
            # through handle_limit_exceeded instead of flat-aborting.
            on_limit=getattr(self._safety, "on_limit", None),
            # 0062: None for every Session except a schema-bearing agent-step spawn
            # (configure_structured_output called before this run_turn).
            response_format=self._response_format,
            schema_validate_fn=self._schema_validate_fn,
            max_schema_reprompt_attempts=self._max_schema_reprompt_attempts,
            # #1909 / #3501 (OPT-IN, default off —
            # safety.threat_scan.capability_narrowing): only thread the
            # per-iteration re-resolve callable (+ its un-narrowed identity
            # baseline) into RouterLoop at the TOP rung of the ladder
            # (``iteration``). At ``off`` and ``turn`` neither kwarg is passed
            # (both stay None on the RouterLoop side) → RouterLoop's
            # per-iteration re-resolve block is a no-op, and the narrowing is
            # whatever the turn-boundary resolve produced.
            **(
                {
                    "intra_turn_contextual_for_turn_fn": self._contextual_for_turn_fn,
                    "contextual_static_baseline": self._contextual_permission,
                }
                if (
                    _narrowing_per_iteration(self._safety)
                    and self._contextual_for_turn_fn is not None
                )
                else {}
            ),
        )
        if self._loop_observer:
            self._loop_observer(loop)
        # PR-N3 / #5528: force-close (never compact) an oversized new
        # message before the turn starts — the proactive, estimate-based
        # history compaction this call used to ALSO trigger is removed
        # (see ContextBudgetAdvisor's own module docstring).
        await self._budget_advisor.enforce_new_msg_budget(new_msg_text=user_text)

        # #4381 PR-4 (owner ruling, verbatim: "２の force close 廃止して
        # spill にしよう。予算のための force close は残すで良い"): the
        # #1092 PR-F2b bounded handoff-and-retry loop is gone — an overflow
        # that survives ``_run_with_shrink``'s own shrink attempts is
        # unrecovered on the FIRST attempt, no consolidating retry. The
        # #4381 family (tool-result spill) is what now keeps an
        # oversized single result from reaching this point at all.
        try:
            # #5296 PR-2 / #5364 §1.6: was a bare
            # `self._run_with_shrink(loop, user_text)` — this wrapper adds
            # same-turn spill recovery for an UnrecoveredError, mode-
            # independent (byte-limited HTTP 413 OR a non-byte token-axis
            # terminal cause — §1.6 dropped the OLD byte-only gate), then
            # re-tries THIS call, bounded by candidate exhaustion (not a
            # fixed attempt count anymore) instead of always failing the
            # turn on the first overflow that survives `_run_with_shrink`'s
            # own shrink attempts. See that method's own docstring for the
            # full contract.
            router_usage = await self._run_with_shrink_and_byte_reduction(
                loop, user_text, chain_id=chain_id,
            )
        # #4885: widened from `_ContextOverflowError` alone — `_run_with_
        # shrink` no longer merges `_UnrecoveredError` (shrinking recovered
        # the SAME cause repeatedly without resolving it, e.g. an HTTP 413
        # request-body-byte limit) into `_ContextOverflowError` (the context
        # window itself is too small). Both still reach this ONE event with
        # the SAME schema — `repr(_overflow_exc)` now correctly names
        # whichever one actually happened instead of always the latter.
        except (_ContextOverflowError, _UnrecoveredError) as _overflow_exc:
            # #5588: `terminal` is the RetryLoopTerminal member (MID_FLOOR/
            # ROOM_FLOOR) when this is an UnrecoveredError -- omitted
            # (never fabricated as a string) for a plain ContextOverflowError,
            # which carries no ladder-terminal distinction. Lets a consumer
            # (the shrink-flow progress display) name which impossibility
            # fired WITHOUT parsing `error`'s repr() text (architect ruling,
            # #5588: "reason 文字列を解析しないこと").
            _terminal = getattr(_overflow_exc, "terminal", None)
            self._events.emit(
                "router_context_overflow_unrecovered",
                error=repr(_overflow_exc),
                # .value (a plain str), not the raw Enum member -- this dict
                # gets json.dumps'd verbatim by EventStore, and RetryLoopTerminal
                # is a bare enum.Enum (not IntEnum/StrEnum), not JSON-safe as-is.
                terminal=_terminal.value if _terminal is not None else None,
            )
            raise

        # F4 Bug 2 / F4 Bug 1: accumulate router LLM usage into per-session
        # totals via the gateway.
        if router_usage is not None:
            self._budget.add_router_usage(
                usage=router_usage,
                last_call_usage=loop.last_call_usage,
                resolver=self._resolver,
                router_model_name=loop.router_model,
            )
