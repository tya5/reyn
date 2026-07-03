"""ChatLifecycleForwarder — session-scoped event subscriber for non-skill events.

This forwarder bridges **session-level lifecycle events** into the chat
outbox:

  * ``compaction_completed`` (issue #162) — head/body/tail compaction
    just replaced N early-session turns with a rolling summary. Without
    a marker the user has no signal that pre-seq-M turns are now a
    summarised proxy.

Designed for growth — additional lifecycle handlers (attach / detach
notifications, budget warnings, session-level errors) can land here
without expanding the skill forwarder's per-skill contract.

Wired up in :class:`reyn.runtime.session.Session` via
``self._chat_events.add_subscriber(ChatLifecycleForwarder(self.outbox))``.
"""
from __future__ import annotations

import asyncio

from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


class ChatLifecycleForwarder:
    """Callable subscriber that bridges session-level events into the outbox."""

    def __init__(self, outbox: asyncio.Queue) -> None:
        self.outbox = outbox

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

    def on_compaction_failed(self, data: dict) -> None:
        """Surface a ``[✗ compaction failed: <reason>]`` error marker.

        ``compaction_controller.py`` emits ``compaction_failed`` when the
        summarisation LLM call raises. Without this handler the user sees the
        ``compaction_started`` side-effect (spinner clears) but gets no signal
        that compaction silently failed — early turns are still unsummarised and
        context pressure continues unrelieved.
        """
        reason = str(data.get("error") or "unknown error")
        self._enqueue(f"[✗ compaction failed: {reason}]")

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
