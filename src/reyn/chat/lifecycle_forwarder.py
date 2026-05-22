"""ChatLifecycleForwarder — session-scoped event subscriber for non-skill events.

Sibling of :class:`reyn.chat.forwarder.ChatEventForwarder`.  Where the
skill forwarder bridges per-skill events (``phase_*`` / ``workflow_*`` /
``llm_*`` / ``act_executed``) into the chat outbox, this forwarder
bridges **session-level lifecycle events** that are not tied to a
specific skill run:

  * ``compaction_completed`` (issue #162) — head/body/tail compaction
    just replaced N early-session turns with a rolling summary. Without
    a marker the user has no signal that pre-seq-M turns are now a
    summarised proxy.

Designed for growth — additional lifecycle handlers (attach / detach
notifications, budget warnings, session-level errors) can land here
without expanding the skill forwarder's per-skill contract.

Wired up in :class:`reyn.chat.session.ChatSession` via
``self._chat_events.add_subscriber(ChatLifecycleForwarder(self.outbox))``.
"""
from __future__ import annotations

import asyncio

from reyn.chat.outbox import OutboxMessage
from reyn.schemas.models import Event


class ChatLifecycleForwarder:
    """Callable subscriber that bridges session-level events into the outbox."""

    def __init__(self, outbox: asyncio.Queue) -> None:
        self.outbox = outbox

    def __call__(self, event: Event) -> None:
        handler = getattr(self, f"on_{event.type}", None)
        if handler:
            handler(event.data)

    # ── Hot list (issue #192) ────────────────────────────────────────────

    def on_hot_list_updated(self, data: dict) -> None:
        """Forward the new full ranking to the outbox as a structured signal.

        Emits ``OutboxMessage(kind="hot_list_updated", text="",
        meta={"ranking": [...]})`` carrying the full sorted ranking
        ``[{qualified_name, freq, last_ts}, ...]``. The Memory tab (=
        tui-coder follow-up) subscribes to this kind to refresh its
        per-entry "hot" badges / sub-section without polling.

        ``text`` is empty because this is a data signal, not a display
        line — the conv pane's ``_format_message`` falls through to its
        unknown-kind handler which is suppressed for non-display kinds.
        """
        ranking = data.get("ranking") or []
        try:
            self.outbox.put_nowait(OutboxMessage(
                kind="hot_list_updated",
                text="",
                meta={"ranking": list(ranking)},
            ))
        except asyncio.QueueFull:
            pass

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

    # ── Compaction (issue #162) ──────────────────────────────────────────

    def on_compaction_completed(self, data: dict) -> None:
        """Surface a ``[↑ N turns compacted]`` marker in the conv pane.

        ``new_turn_count`` is the count of turns replaced by the rolling
        summary.  Falls back to a generic marker when the field is
        absent (= forward-compat with future event-shape variations).
        """
        count = data.get("new_turn_count")
        if count:
            text = f"[↑ {count} turn{'s' if count != 1 else ''} compacted]"
        else:
            text = "[↑ history compacted]"
        self._enqueue(text)

    def _enqueue(self, text: str) -> None:
        # Fire-and-forget: lifecycle markers are advisory, never block the
        # session loop. Uses ``kind="system"`` so the conv pane's
        # ``_render_system_message`` path styles it as a dim marker line.
        try:
            self.outbox.put_nowait(OutboxMessage(kind="system", text=text))
        except asyncio.QueueFull:
            pass

    # ── Tool-call lifecycle (issue #427 wiring fix 2026-05-22) ───────────
    # ``dispatch/dispatcher.py:200-274`` emits ``tool_called`` /
    # ``tool_returned`` / ``tool_failed`` against the session's
    # ``_chat_events`` log (= router-level). This forwarder is the
    # subscriber of that log; per-skill ``ChatEventForwarder`` only sees
    # the per-skill agent's event log and would never fire on these.
    # Step 3 of issue #427 originally landed the handlers on the wrong
    # forwarder class — wave-#427 smoke detected the gap, this PR moves
    # them to the correct subscriber. See memory
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

    def _enqueue_tool_call(
        self,
        *,
        kind: str,
        data: dict,
        extra_meta: dict,
    ) -> None:
        """Shared enqueue path for the three tool-call lifecycle outbox kinds.

        Session-level forwarder has no own ``run_id`` / ``skill_name`` to
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
        }
        # Surface run_id when present so consumers can attribute the
        # row to a parent skill thread (= sub-skill spawned tool calls
        # carry the spawned skill's run_id from the dispatcher's caller_id).
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


__all__ = ["ChatLifecycleForwarder"]
