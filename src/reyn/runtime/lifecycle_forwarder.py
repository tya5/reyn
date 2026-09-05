"""ChatLifecycleForwarder — session-scoped event subscriber for lifecycle events.

This forwarder bridges **session-level lifecycle events** into the chat
outbox:

  * ``compaction_completed`` (issue #162) — head/body/tail compaction
    just replaced N early-session messages with a rolling summary
    (#5791: a message count, not a turn count — see this module's own
    ``on_compaction_completed`` docstring). Without a marker the user
    has no signal that pre-seq-M history is now a summarised proxy.

Designed for growth — additional lifecycle handlers (attach / detach
notifications, budget warnings, session-level errors) can land here
without expanding the lifecycle forwarder's per-handler contract.

Wired up in :class:`reyn.runtime.session.Session` via
``self._audit_events.add_subscriber(ChatLifecycleForwarder(self.outbox, registry=self._registry))``.
The optional ``registry`` lets a handler bridge-subscribe to another session's
own EventLog (#2570: a pipeline driver-session's live step progress).
"""
from __future__ import annotations

import asyncio
from typing import Any

from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


def _compact_token_count(n: int) -> str:
    """A compact token count for a lifecycle marker — ``"812"`` / ``"8.2k"`` /
    ``"120k"``. Deliberately a SMALL, self-contained duplicate of
    ``gutter.py``'s own ``_format_tokens`` rather than an import of it:
    ``gutter.py`` is UI-layer (``interfaces.inline.textual_chat``) and this
    module is runtime-layer, so importing it here would invert the
    dependency direction. #4703 axis①."""
    n = max(0, n)
    if n < 1_000:
        return str(n)
    if n < 9_950:
        return f"{n / 1_000:.1f}k"
    return f"{round(n / 1_000)}k"


class ChatLifecycleForwarder:
    """Callable subscriber that bridges session-level events into the outbox."""

    def __init__(
        self,
        outbox: asyncio.Queue,
        registry: "Any | None" = None,
        events: "Any | None" = None,
    ) -> None:
        self.outbox = outbox
        self._registry = registry
        # #2708 P3.1 Half-B: this forwarder's OWN session EventLog (the parent/caller
        # audit log). Used by the driver→parent bridge to re-emit a driver-session's
        # ``presented`` event onto the parent's log with ``bridged_from=<driver_sid>``,
        # so the present audit trail is not split across the driver and parent sessions.
        # None (pre-P3.1 callers / tests) simply skips the audit-forward (visible-output
        # bridge is independent, established by consumer inheritance, not here).
        self._events = events
        # #2570: run_id -> (driver EventLog, listener fn, invoking tool name).
        # Tracks bridge-subscriptions to a pipeline driver-session's own
        # EventLog for the duration of one sync-attached run_pipeline call.
        self._pipeline_subs: dict[str, tuple[Any, Any, str | None]] = {}

    def __call__(self, event: Event) -> None:
        handler = getattr(self, f"on_{event.type}", None)
        if handler:
            handler(event.data)

    # ── Budget warn (wave-5 C5) ──────────────────────────────────────────

    def on_budget_warn(self, data: dict) -> None:
        """Surface a ``[↑ budget warn: <dimension> (N%)]`` marker in the conv pane.

        The Events tab colour-codes ``budget_warn`` in yellow, but a user
        with the side panel closed (= the default) sees nothing — the
        budget can silently approach its cap without any signal in the
        conv pane. Mirror the ``on_compaction_completed`` pattern: emit a
        lifecycle marker (``[↑ … ]``) so the conv pane's
        ``_render_lifecycle_marker`` route displays it as a dim inline
        divider, matching the compaction marker's visual weight.

        ``data["dimension"]`` names the warned axis (``daily_tokens`` /
        ``daily_cost_usd`` / etc.). ``data["current"]`` and
        ``data["hard"]`` are the snapshot from ``BudgetCheck.context``;
        when both are numeric we surface a ``(N%)`` annotation so the
        user can see how close they are to the cap.
        """
        dim = str(data.get("dimension") or "budget")
        current = data.get("current")
        hard = data.get("hard")
        pct_part = ""
        try:
            if (
                isinstance(current, (int, float))
                and isinstance(hard, (int, float))
                and hard > 0
            ):
                pct = int(round((float(current) / float(hard)) * 100))
                pct_part = f" ({pct}%)"
        except Exception:
            pct_part = ""
        self._enqueue(f"[↑ budget warn: {dim}{pct_part}]")

    # ── High-cost model pre-selection warn (#1830 / FP-0052) ─────────────

    def on_model_cost_warn(self, data: dict) -> None:
        """Surface a ``[⚠ high-cost model: …]`` marker in the conv pane.

        Mirrors ``on_budget_warn``: the Events tab surfaces ``model_cost_warn``
        in yellow automatically, but the conv pane needs an explicit marker so
        the user sees the warning without having the side panel open.

        ``data["model"]`` is the resolved litellm model string.
        ``data["cost_per_1m_input_usd"]`` is the per-1M-token input rate.
        ``data["threshold_per_1m_input_usd"]`` is the configured threshold.
        """
        model = str(data.get("model") or data.get("model_class") or "unknown")
        cost = data.get("cost_per_1m_input_usd")
        try:
            cost_str = f"${float(cost):.2f}/1M input tokens" if cost is not None else ""
        except (TypeError, ValueError):
            cost_str = ""
        suffix = f" — {cost_str}" if cost_str else ""
        self._enqueue(f"[⚠ high-cost model: {model}{suffix}]")

    def on_model_cost_block(self, data: dict) -> None:
        """Surface a ``[✗ model switch declined: …]`` marker when the user
        rejects the high-cost model confirm (#1867 / FP-0052 S4).

        Only fires on ``reason="declined"`` (= the user said No). Approved
        switches need no extra message — the status-bar chip updates to show
        the new model. Non-interactive fail-closed (no human present) is also
        silent.
        """
        if data.get("reason") != "declined":
            return
        model = str(data.get("model") or data.get("model_class") or "unknown")
        self._enqueue(f"[✗ model switch declined: {model}]")

    # ── Config hot-reload (#2073) ──────────────────────────────────────────

    def on_config_reloaded(self, data: dict) -> None:
        """Surface a ``[↻ config reloaded: <components>]`` marker in the conv pane.

        Fires after a hot-reload applies at the turn boundary (#2073 S1). A user
        who ran ``/reload`` gets confirmation that the reload completed and which
        components changed. Silenced when no component reported a change AND no
        seam failed — a reload that touched nothing is already confirmed by the
        ``/reload`` reply; a second no-op marker would be noise.

        ``data["components"]`` is the list of seam names that reported a change
        (e.g. ``["hooks", "mcp"]``). ``data["failed"]`` is the list of seams
        that raised an exception.
        """
        applied = list(data.get("components") or [])
        failed = list(data.get("failed") or [])
        if not applied and not failed:
            return
        parts: list[str] = []
        if applied:
            parts.append(", ".join(applied))
        if failed:
            parts.append(f"✗ failed: {', '.join(failed)}")
        self._enqueue(f"[↻ config reloaded: {'; '.join(parts)}]")

    def on_config_reload_rejected(self, data: dict) -> None:
        """Surface a ``[✗ config reload rejected: <reason>]`` error marker.

        Fires when the validate-before-apply step rejects the IN-set as
        malformed (#2073 S2). Without this marker, the user sees the ``/reload``
        "scheduled" confirmation and then nothing — the next turn silently
        runs under the OLD config, with only a ``_log.warning`` that is never
        visible in the inline CUI.
        """
        reason = str(data.get("reason") or "malformed config")
        self._enqueue(f"[✗ config reload rejected: {reason}]")

    # ── Compaction (issue #162) ──────────────────────────────────────────

    def on_compaction_started(self, data: dict) -> None:
        """Surface a ``[⟳ compacting N messages]`` marker when a real
        compaction pass begins (#5633 — owner: "縮小フロー開始開始通知みたいな
        ものは tui で受けてないのかしら？").

        Before this handler, ``completed``/``failed`` both had a marker but
        ``started`` had none — asymmetric, and the gap this issue's own
        finding names: the event was emitted (``engine.py``'s
        ``compact()``), nothing in ``src/reyn/interfaces/`` ever consumed it.

        #5588 correction: this marker is no longer independent of #5618/
        #5630's `is_compacting`/`recovery_episode` state — owner's own
        "開始〜終了は単一 flowview entry or group にして" ruled that a
        `compact()` call inside an already-running shrink-flow episode
        (#5719's own shrink-retry ladder can call `compact()` more than
        once per episode) must NOT scatter its own edge marker into the
        conv pane as a separate line. This handler still ALWAYS emits —
        ``meta["compaction_episode_marker"]`` is what lets ``app.py``'s
        ``_ingest_frame`` ABSORB the frame into the single open episode
        entry (TUI-local; the frame still reaches the outbox unchanged,
        so a surface with no episode-entry mechanism of its own, e.g.
        AG-UI, still gets this marker exactly as before).

        #5791 (owner-hit, real machine: ``[⟳ compacting 3925 turns]`` shown
        against a 73-user-message history): this field is
        ``new_message_count`` — ``engine.py``'s own count of wire messages
        in the input chunk (every role, mixed). This is the SAME quantity
        ``on_compaction_completed``'s own ``new_message_count`` below
        measures (a different producer, ``CompactionController`` — its own
        candidate selection never groups messages into turns either,
        despite a ``turns``-named local variable there; BLOCKING-corrected
        mid-review, the same "trusted the name, not the code" misreading
        as #5592's own incident and this issue's own owner-hit). The two
        used to share one field name (``new_turn_count``) that was wrong
        for BOTH producers, not different-but-each-correct. Degrades to a
        generic marker when absent, never a fabricated count."""
        count = data.get("new_message_count")
        meta = {"compaction_episode_marker": True}
        if isinstance(count, int) and count > 0:
            self._enqueue(f"[⟳ compacting {count} message{'s' if count != 1 else ''}]", meta=meta)
        else:
            self._enqueue("[⟳ compacting history]", meta=meta)

    def on_compaction_failed(self, data: dict) -> None:
        """Surface a ``[✗ compaction failed: <reason>]`` error marker.

        ``compaction_controller.py`` emits ``compaction_failed`` when the
        summarisation LLM call raises. Without this handler the user sees the
        ``compaction_started`` marker (#5633) but gets no signal that
        compaction silently failed — early turns are still unsummarised and
        context pressure continues unrelieved.
        """
        reason = str(data.get("error") or "unknown error")
        self._enqueue(
            f"[✗ compaction failed: {reason}]",
            meta={"compaction_episode_marker": True},
        )

    def on_router_context_overflow_unrecovered(self, data: dict) -> None:
        """Surface a ``[✗ shrink flow failed: <impossibility>]`` marker —
        the TRUE end-of-episode failure, distinct from :meth:`on_compaction_
        failed` above (a single ``compact()`` call raising, which #5719's
        own shrink-retry ladder may still recover from within the SAME
        episode — this event fires only once the whole ladder is
        exhausted).

        Names WHICH impossibility fired via the ``RetryLoopTerminal``
        member itself (#5588 architect ruling: "reason 文字列を解析しない
        こと") — never a parse of ``error``'s own ``repr()`` text. A plain
        ``ContextOverflowError`` (the window itself is too small — no
        ladder-terminal distinction at all) carries no ``terminal`` field
        (never fabricated) and degrades to a generic marker.

        The 2-line mapping is a SMALL, deliberate duplicate of
        ``compaction_progress.py``'s own ``compaction_failure_text`` —
        that module is INTERFACES-layer (imports ``textual``); this one is
        RUNTIME-layer, so importing it here would invert the dependency
        direction (same reasoning this module's own ``_compact_token_
        count`` docstring already gives for not importing ``gutter.py``).
        """
        terminal = data.get("terminal")
        text = {
            "mid_floor": "1つのやり取りが単独で大きすぎます",
            "room_floor": "最新のメッセージだけで窓に入りません",
        }.get(str(terminal))
        if text is not None:
            self._enqueue(f"[✗ shrink flow failed: {text}]")
        else:
            self._enqueue("[✗ shrink flow failed]")

    def on_summary_resummarize_failed(self, data: dict) -> None:
        """Surface a ``[✗ summary re-compress failed: <reason>]`` error marker.

        ``compaction/engine.py`` calls ``_resummarize_topic_arc`` when the
        produced topic_arc overshoots its body-budget (T2 re-compression
        pass). When that LLM call raises, the engine catches it, emits
        ``summary_resummarize_failed``, and falls back to the uncompressed
        arc — which may still overshoot. Without this handler the user sees
        ``compaction_completed`` as if everything succeeded, but the stored
        summary is potentially larger than the budget, degrading future
        compaction quality silently.
        """
        reason = str(data.get("error") or "unknown error")
        self._enqueue(f"[✗ summary re-compress failed: {reason}]")

    def on_compaction_completed(self, data: dict) -> None:
        """Surface a ``[↑ N messages compacted]`` marker in the conv pane.

        ``new_message_count`` is the count of wire messages replaced by the
        rolling summary. Falls back to a generic marker when the field is
        absent (= forward-compat with future event-shape variations).

        #5791 (BLOCKING correction, lead-coder review of this PR's own
        first pass — which had claimed this WAS a genuine turn count):
        ``CompactionController``'s own ``_select_candidates`` never groups
        messages into turns — its ``turns`` parameter (the source of this
        method's misleading former name, ``new_turn_count``) is itself a
        flat list of individual ``ChatMessage`` entries. This field is the
        SAME quantity as ``on_compaction_started``'s own
        ``new_message_count`` above (a different producer, ``engine.py``)
        — the two were never actually different quantities, only
        differently-misnamed instances of the same one. Both markers now
        say "messages", never "turns".

        #4703 axis①: the marker also names what the compaction LLM call
        itself SPENT (``prompt_tokens``/``completion_tokens``/``cost_usd``,
        CompactionController's own recent addition) — owner's own
        complaint: this row already existed, it just never showed real
        money, an off-screen high-cost call the conversation face was
        silent about. Absent (pre-#4703-shape events, or usage genuinely
        unreadable off the response) degrades to the pre-#4703 marker —
        never a fabricated ``$0.00``."""
        count = data.get("new_message_count")
        if count:
            text = f"[↑ {count} message{'s' if count != 1 else ''} compacted"
        else:
            text = "[↑ history compacted"
        prompt = data.get("prompt_tokens")
        completion = data.get("completion_tokens")
        if isinstance(prompt, int) and isinstance(completion, int):
            # Same ↑prompt/↓completion glyph convention gutter.py's
            # ReynTurnUsageGutter already uses for a conversational row —
            # a reader who has learned that convention reads this
            # off-conversation marker the same way, no new vocabulary.
            text += f" · ↑{_compact_token_count(prompt)} ↓{_compact_token_count(completion)}"
        cost = data.get("cost_usd")
        if isinstance(cost, (int, float)):
            text += f" · ${cost:.2f}"
        text += "]"
        # #5588: same absorption tag as on_compaction_started/failed above
        # — a shrink-flow episode can call compact() more than once (#5719's
        # own shrink-retry ladder), so this must not scatter either, even
        # though its OWN text is architect's explicitly-unchanged success
        # format (still built exactly as before this PR).
        self._enqueue(text, meta={"compaction_episode_marker": True})

    # ── Router cap / iteration limit ─────────────────────────────────────
    # Two distinct ``limit_denied`` sources:
    #   kind="router_cap"     — session.py, op-count exceeds operator cap
    #   kind="max_iterations" — router_loop.py, iteration ceiling reached

    def on_limit_denied(self, data: dict) -> None:
        """Surface a ``[✗ … limit hit]`` marker distinguishing the two cap kinds.

        ``router_cap`` fires when the loop's tool-call count exceeds the
        operator-configured cap (``safety.router_cap``); ``count`` and ``cap``
        carry the numbers. ``max_iterations`` fires when the router's iteration
        ceiling is hit; ``limit`` carries the configured maximum. Without this
        handler the user only sees whatever LLM wrap-up text the session
        synthesises — no inline marker signals that the cap is WHY the turn
        ended early.
        """
        kind = data.get("kind", "")
        if kind == "max_iterations":
            limit = data.get("limit")
            if limit is not None:
                self._enqueue(f"[✗ iteration limit hit: {limit} iterations]")
            else:
                self._enqueue("[✗ iteration limit hit]")
        else:
            count = data.get("count")
            cap = data.get("cap")
            if count is not None and cap is not None:
                self._enqueue(f"[✗ router cap hit: {count} ops (limit {cap})]")
            else:
                self._enqueue("[✗ router cap hit]")

    # ── Force close (#4380) ────────────────────────────────────────────
    # #4381 PR-4 (owner ruling, verbatim: "２の force close 廃止して spill
    # にしよう。予算のための force close は残すで良い") removed the second
    # of two mechanisms this comment used to distinguish — layer②
    # (``router_force_close_handoff``, router_loop_driver.py's OUTER retry
    # when the wrap-up itself still didn't fit) is gone; the overflow case
    # it used to catch is now a tool-result spill (#4381 family) instead of
    # a history-consolidating handoff. Only layer① remains:

    def on_force_close_triggered(self, data: dict) -> None:
        """Surface a ``[✗ force close: …]`` marker — the in-loop,
        per-iteration cumulative-budget cutoff (``should_force_close``).

        ``should_force_close`` decided the accumulated cost/tokens already
        exceed the session's cumulative cap, so this turn's wrap-up
        suppresses further tool calls and ends early. Without this marker
        a short/truncated-looking reply has no visible reason attached to
        it — the user sees a turn end, not WHY.
        """
        self._enqueue("[✗ force close: turn ended early to stay within budget]")

    # ── Turn cancelled (#4380) ────────────────────────────────────────────

    def on_turn_cancelled(self, data: dict) -> None:
        """Surface a ``[✗ turn cancelled]`` marker.

        Before #4380 this had ZERO live visibility: ``router_loop.py``'s own
        cancel path persists "Turn interrupted by user." via
        ``append_history_entry``, which is explicitly documented as having
        "no outbox side-effect" (``router_host_adapter.py``) — visible only
        on the NEXT restore (``restore.py`` rescues the ``meta["kind"] ==
        "turn_cancelled"`` entry from the blanket system/summary skip). A
        user watching the CURRENT session saw nothing at all when they
        cancelled a turn. This is a genuinely new gap, not a duplicate of
        that history entry — the two never both render in the same
        session's lifetime (this one live, that one only after a restart).
        """
        self._enqueue("[✗ turn cancelled]")

    # ── Chain timeout (#4380) ────────────────────────────────────────────

    def on_chain_timeout(self, data: dict) -> None:
        """Surface a ``[✗ chain timeout: waiting on …]`` marker.

        ``waiting_on`` (sorted agent list) + ``timeout_seconds`` come
        straight off the audit event (``docs/reference/runtime/events.md``'s
        own documented payload for this kind). A turn that spawned MULTIPLE
        concurrent delegate chains can fire this once per chain that times
        out — each is its own independent event (different ``chain_id``,
        different agents waited on), so each gets its OWN marker, never
        bundled with another chain's timeout (#4380: only same-(kind, path,
        reason) ``permission_denied`` bundles; this does not).
        """
        waiting = data.get("waiting_on") or []
        agents = ", ".join(str(a) for a in waiting) if waiting else "?"
        timeout_s = data.get("timeout_seconds")
        suffix = f" ({timeout_s}s)" if timeout_s is not None else ""
        self._enqueue(f"[✗ chain timeout: waiting on {agents}{suffix}]")

    # ── Permission / intervention denied (#4380) ─────────────────────────

    def on_permission_denied(self, data: dict) -> None:
        """Surface a ``[✗ permission denied: …]`` marker — the ONE kind of
        this issue's 6 that BUNDLES repeats (owner ruling, #4380): a turn
        that hits the same denial (same op ``kind``, same ``path``, same
        ``reason``) repeatedly collapses to one line with a count on the
        conv-pane side (:meth:`~reyn.interfaces.inline.textual_chat.app.
        TextualChatApp._ingest_frame`'s ``_last_lifecycle_marker`` coalesce —
        mirrors ``_coalesce_tool_result``'s own precedent: compare only
        against the immediately-preceding row, never a turn-wide buffer).
        A DIFFERENT ``path`` or ``reason`` for the SAME op ``kind`` is a
        DIFFERENT fact and must NOT collapse into the same line (owner's
        own constraint, applied literally) — the bundle key carries all
        three fields for exactly that reason. The audit log itself is
        UNCHANGED — one event per denial, always; only the DISPLAY
        collapses consecutive identical ones.
        """
        kind = str(data.get("kind") or "?")
        path = data.get("path")
        reason = str(data.get("reason") or "")
        suffix = f" {path}" if path else ""
        text = f"[✗ permission denied: {kind}{suffix}]"
        self._enqueue(
            text,
            meta={"lifecycle_bundle_key": ("permission_denied", kind, path, reason)},
        )

    def on_intervention_denied(self, data: dict) -> None:
        """Surface a ``[✗ intervention denied: …]`` marker — deliberately
        NOT bundled (#4380 ruling): a different ``intervention_id`` is a
        different unanswered question, even when the ``kind`` (ask_user /
        permission / safety-limit) matches — collapsing those would lose
        which questions went unanswered, not just how many.
        """
        kind = str(data.get("kind") or "?")
        self._enqueue(f"[✗ intervention denied: {kind}]")

    def _enqueue(self, text: str, *, meta: "dict | None" = None) -> None:
        # Fire-and-forget: lifecycle markers are advisory, never block the
        # session loop. Uses ``kind="system"`` so the conv pane's
        # ``_render_system_message`` path styles it as a dim marker line.
        # ``meta`` (#4380): optional — ``on_permission_denied`` (below)
        # still stamps ``lifecycle_bundle_key`` on every call. #5588
        # correction: the conv-pane consumer that once coalesced consecutive
        # repeats keyed on it was ITSELF removed as unreachable (``fix
        # (#4380): remove the unreachable permission_denied ×N bundling`` —
        # re-measured, no live trigger ever produced two adjacent
        # occurrences). The write here is now INERT — no reader consumes
        # this key anywhere in the tree (verified: `git grep
        # lifecycle_bundle_key` finds only this write site) — kept
        # (not removed) because whether to also drop it is a separate,
        # out-of-scope decision from the one #5588 needed to make (CLAUDE.md:
        # a doc/comment goes stale the moment the mechanism it describes
        # changes; fixed here in the same PR that found it, per that rule —
        # the writer itself is untouched).
        try:
            self.outbox.put_nowait(OutboxMessage(kind="system", text=text, meta=meta or {}))
        except asyncio.QueueFull:
            pass

    # ── Tool-call lifecycle (issue #427 wiring fix 2026-05-22) ───────────
    # ``dispatch/dispatcher.py:200-274`` emits ``tool_called`` /
    # ``tool_returned`` / ``tool_failed`` against the session's
    # ``_audit_events`` log (= router-level). This forwarder is the
    # subscriber of that log. See memory
    # ``feedback_verify_existing_event_emission_before_adding`` for the
    # subscriber-layer verification discipline.

    def on_tool_called(self, data: dict) -> None:
        """Bridge ``dispatch_tool``'s pre-event into a ``tool_call_started``
        outbox message.

        Source schema (= ``dispatch/dispatcher.py:200``):
            {caller_kind, caller_id, tool, chain_id, args, args_hash}

        ``args_hash`` is the deterministic correlation id we hand to the
        TUI widget so it can match the eventual ``tool_call_completed`` /
        ``tool_call_failed`` to this mount call.
        """
        self._enqueue_tool_call(
            kind="tool_call_started",
            data=data,
            extra_meta={"args": data.get("args")},
        )

    def on_tool_returned(self, data: dict) -> None:
        """Bridge ``dispatch_tool``'s post-event into a ``tool_call_completed``
        outbox message.

        Source schema (= ``dispatch/dispatcher.py:262``):
            {caller_kind, caller_id, tool, chain_id, args_hash, result}
        """
        self._enqueue_tool_call(
            kind="tool_call_completed",
            data=data,
            extra_meta={"result": data.get("result")},
        )
        result = data.get("result")
        run_id = result.get("run_id") if isinstance(result, dict) else None
        self._maybe_unsubscribe_pipeline(data.get("tool"), run_id)

    def on_tool_failed(self, data: dict) -> None:
        """Bridge ``dispatch_tool``'s failure event into a ``tool_call_failed``
        outbox message.

        Source schema (= ``dispatch/dispatcher.py:222``):
            {caller_kind, caller_id, tool, chain_id, args_hash, error_kind, message}
        """
        self._enqueue_tool_call(
            kind="tool_call_failed",
            data=data,
            extra_meta={
                "error_kind": data.get("error_kind"),
                "error_message": data.get("message"),
            },
        )
        # No result dict on a raised exception (dispatcher never reached the
        # handler's return) — fall back to matching by tool name.
        self._maybe_unsubscribe_pipeline(data.get("tool"), None)

    # ── Pipeline attached live-progress bridge (#2570) ────────────────────
    # session_api.py's run_pipeline_attached emits pipeline_run_attached onto
    # THIS session's own _audit_events right after spawning the driver-session
    # (sync-attached path only). The driver-session's pipeline_step_started /
    # pipeline_step_completed events land on ITS OWN EventLog — a session
    # distinct from this one, invisible here unless we bridge-subscribe.

    def on_pipeline_run_attached(self, data: dict) -> None:
        """Bridge-subscribe to the driver-session's EventLog for one run's duration.

        ``data`` = {tool, run_id, driver_sid, agent_name, pipeline_name} (see
        ``session_api.run_pipeline_attached``). Looks up the driver session via
        the injected registry and forwards its ``pipeline_step_started`` /
        ``pipeline_step_completed`` events (matched by ``run_id``) as transient
        ``status`` lines — mirroring ``on_mcp_progress``: a many-step pipeline
        would spam permanent ``system`` markers otherwise. Unsubscribed by
        ``on_tool_returned`` / ``on_tool_failed`` when the matching
        ``run_pipeline`` tool call completes. No-ops gracefully if the registry
        is absent or the driver session can't be found (forward-compat with
        event-shape drift, same idiom as the rest of this forwarder)."""
        if self._registry is None:
            return
        tool = data.get("tool")
        run_id = data.get("run_id")
        driver_sid = data.get("driver_sid")
        agent_name = data.get("agent_name")
        pipeline_name = str(data.get("pipeline_name") or "pipeline")
        if not (run_id and driver_sid and agent_name):
            return
        driver_session = self._registry.get_session(agent_name, driver_sid)
        if driver_session is None:
            return
        driver_events = getattr(getattr(driver_session, "router_host", None), "events", None)
        if driver_events is None:
            return

        def _on_driver_event(event: Event) -> None:
            if event.type == "presented":
                # #2708 P3.1 Half-B: bridge the driver's ``presented`` P6 audit event
                # onto the PARENT's EventLog. Deliberately NOT run_id-filtered (unlike
                # the pipeline_step_* path below), and this is SAFE — not the mis-fix of
                # dropping a shared filter that guards concurrency:
                #   (a) a pipeline-step present's ``run_id`` is structurally None, never
                #       the pipeline rid — present.py:116 builds its OpContext via
                #       ``ctx.router_state.op_context_factory()`` (the driver session's
                #       ``make_router_op_context``, session.py:1739, run_id=None), and
                #       op_runtime/present.py:132 emits ``presented`` with that run_id.
                #       A run_id-equality filter would therefore SILENTLY DROP every
                #       bridged present (the architect's flagged failure mode).
                #   (b) there is no cross-run leak to guard against here: each pipeline
                #       run spawns its OWN driver session (session_api._spawn_pipeline_
                #       driver_session → a fresh spawn_session_recorded), so this
                #       per-driver-EventLog subscription — added on attach, removed on
                #       tool completion — sees exactly ONE run's presents. The driver
                #       session runs only the pipeline (a PipelineExecutorDriver loop, no
                #       interactive chat), so no unrelated present rides this log.
                # (Making ``presented.run_id`` carry the rid = a larger present-op change,
                # architect Q1-deferred; this mechanism-level bridge is the P3.1 scope.)
                self._forward_presented_event(event, driver_sid=driver_sid, run_id=run_id)
                return
            if event.type not in ("pipeline_step_started", "pipeline_step_completed"):
                return
            if event.data.get("run_id") != run_id:
                return
            self._enqueue_pipeline_step(pipeline_name, event.type, event.data)

        driver_events.add_subscriber(_on_driver_event)
        self._pipeline_subs[run_id] = (driver_events, _on_driver_event, tool)

    def _enqueue_pipeline_step(self, pipeline_name: str, event_type: str, data: dict) -> None:
        step_index = data.get("step_index")
        total_steps = data.get("total_steps")
        step_kind = data.get("step_kind", "?")
        if event_type == "pipeline_step_started":
            n = (step_index or 0) + 1
            marker, suffix = "▸", ""
        else:
            n = step_index or 0
            marker, suffix = "✓", " done"
        progress = f"{n}/{total_steps}" if total_steps else str(n)
        text = f"[{marker} {pipeline_name}: step {progress} ({step_kind}){suffix}]"
        # The numbers travel in meta, not only inside ``text``: a display that
        # wants a progress bar should read them, never parse them back out of a
        # sentence whose wording is free to change.
        meta = {
            "source": "pipeline",
            "run_id": data.get("run_id"),
            "pipeline_name": pipeline_name,
            "step_index": step_index,
            "total_steps": total_steps,
            "step_kind": step_kind,
            "step_event": event_type,
        }
        try:
            self.outbox.put_nowait(OutboxMessage(kind="status", text=text, meta=meta))
        except asyncio.QueueFull:
            pass

    def _forward_presented_event(
        self, event: Event, *, driver_sid: str, run_id: str
    ) -> None:
        """#2708 P3.1 Half-B: re-emit a driver-session's ``presented`` P6 event onto the
        PARENT's EventLog so the present audit trail is not split across sessions.

        The driver's OWN log legitimately keeps its native copy; this ADDS a copy to the
        parent's log (which previously had none) annotated with provenance so replay/audit
        tooling can tell a bridged present from a native one and not double-count:
        ``bridged_from`` = the driver sid, ``pipeline_run_id`` = the attached run's id
        (distinct from the event's own ``run_id``, which is the chat-router ``None``).

        No-op when the forwarder was built without an ``events`` handle (pre-P3.1 callers
        / tests). The parent forwarder defines no ``on_presented`` handler, so re-emitting
        here does not recurse."""
        if self._events is None:
            return
        payload = dict(event.data)
        payload["bridged_from"] = driver_sid
        payload.setdefault("pipeline_run_id", run_id)
        self._events.emit("presented", **payload)

    def _maybe_unsubscribe_pipeline(self, tool: "str | None", run_id: "str | None") -> None:
        if not self._pipeline_subs:
            return
        target_rid = run_id
        if target_rid is None:
            for rid, (_events, _listener, sub_tool) in list(self._pipeline_subs.items()):
                if sub_tool == tool:
                    target_rid = rid
                    break
        entry = self._pipeline_subs.pop(target_rid, None) if target_rid else None
        if entry is not None:
            events, listener, _ = entry
            events.remove_subscriber(listener)

    def _enqueue_tool_call(
        self,
        *,
        kind: str,
        data: dict,
        extra_meta: dict,
    ) -> None:
        """Shared enqueue path for the three tool-call lifecycle outbox kinds.

        Session-level forwarder has no own ``run_id`` / ``actor`` to
        contribute — every meta field is sourced from the event payload
        itself. Consumers (= the conv pane's ``_on_tool_call_*``) read
        ``meta["op_id"]`` (= the deterministic ``args_hash``) to pair
        start / end events; ``meta["tool"]`` carries the tool name for
        display; ``args`` / ``result`` / ``error_*`` live in the
        kind-specific extras.
        """
        tool_name = str(data.get("tool", ""))
        meta: dict = {
            "tool": tool_name,
            "op_id": data.get("args_hash"),
            "chain_id": data.get("chain_id"),
            "caller_kind": data.get("caller_kind"),
            "caller_id": data.get("caller_id"),
            # #4691 Phase B ①(remainder): the litellm call this tool call
            # belongs to (dispatcher.py's own DispatchContext.call_id,
            # threaded from RouterLoop's current round as an explicit
            # parameter, never a stored field) — the same key
            # llm_response_received carries for that call (#4722). None for
            # a dispatch that never threaded one through (op-loop / non-
            # router callers) — never a minted placeholder.
            "call_id": data.get("call_id"),
        }
        # Surface run_id when present so consumers can attribute the
        # row to a parent agent thread (= sub-agent spawned tool calls
        # carry the spawned run's run_id from the dispatcher's caller_id).
        run_id = data.get("run_id") or data.get("caller_id")
        if run_id:
            meta["run_id"] = run_id
            meta["run_id_short"] = str(run_id)[-4:]
        meta.update(extra_meta)
        try:
            self.outbox.put_nowait(
                OutboxMessage(kind=kind, text=tool_name, meta=meta),
            )
        except asyncio.QueueFull:
            pass

    # ── MCP tool progress (issue #264) ───────────────────────────────────────
    # ``op_runtime/mcp.py`` emits ``mcp_progress`` each time the MCP SDK
    # delivers a ``notifications/progress`` callback during a tool call.
    # Source schema: {server, tool, progress, total, message}

    def on_mcp_progress(self, data: dict) -> None:
        """Bridge ``mcp_progress`` into a ``status`` outbox message.

        Emits ``kind="status"`` with ``meta.source="mcp"`` so the sticky
        status bar shows live MCP tool progress during a long-running call.
        ``meta.source`` discriminates MCP status from other status sources
        for future per-source styling.
        """
        server = str(data.get("server") or "?")
        tool = str(data.get("tool") or "?")
        progress = data.get("progress")
        total = data.get("total")
        message = data.get("message")

        text = _format_mcp_progress(server, tool, progress, total, message)

        meta: dict = {
            "source": "mcp",
            "server": server,
            "tool": tool,
        }
        if progress is not None:
            meta["progress"] = progress
        if total is not None:
            meta["total"] = total
        if message:
            meta["progress_text"] = message

        try:
            self.outbox.put_nowait(OutboxMessage(kind="status", text=text, meta=meta))
        except asyncio.QueueFull:
            pass


def _format_mcp_progress(
    server: str,
    tool: str,
    progress: object,
    total: object,
    message: object,
) -> str:
    """Build the human-readable sticky-status text for an MCP progress event.

    Branches:
      - progress + total both numeric and total > 0 → percentage
      - progress numeric, total absent / zero        → raw progress value
      - neither                                      → bare ``[mcp/<server>] <tool>``
      - message present                              → appended as ``· <message>``
    """
    head = f"[mcp/{server}] {tool}"
    body = ""
    try:
        prog_f: float | None = float(progress) if progress is not None else None
    except (TypeError, ValueError):
        prog_f = None
    try:
        tot_f: float | None = float(total) if total is not None else None
    except (TypeError, ValueError):
        tot_f = None
    if prog_f is not None and tot_f is not None and tot_f > 0:
        pct = (prog_f / tot_f) * 100
        body = f" · {pct:.0f}%"
    elif prog_f is not None:
        body = f" · progress={prog_f:g}"
    text = head + body
    if message:
        text += f" · {message}"
    return text


__all__ = ["ChatLifecycleForwarder", "_format_mcp_progress"]
