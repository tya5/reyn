"""ChatEventForwarder — surface spawned-skill phase transitions in chat.

A spawned skill emits `phase_started` / `phase_completed` events through the
Agent's EventLog. By default those go only to the per-run jsonl persister,
so the chat user sees nothing between `[skill] 起動...` and the final
`agent>` narration.

This subscriber bridges the gap: it filters for the two events that summarize
progress and pushes a one-line `[trace]` message into the chat outbox. Other
event types (LLM calls, act ops, artifact storage, …) stay out of chat to
keep the noise level reasonable — those are all available in the run's
jsonl log if the user wants the full picture.
"""
from __future__ import annotations

import asyncio

from reyn.chat.outbox import OutboxMessage
from reyn.schemas.models import Event


class ChatEventForwarder:
    """Callable subscriber that turns skill events into outbox messages."""

    def __init__(
        self, skill_name: str, outbox: asyncio.Queue, *, run_id: str | None = None,
    ) -> None:
        self.skill_name = skill_name
        self.outbox = outbox
        self.run_id = run_id
        # Issue #214: track which run_ids have already received their
        # "plan N/M" one-shot detail so we don't spam the row on every
        # phase advance (= once per child skill at first phase_started).
        self._plan_step_announced: set[str] = set()

    def __call__(self, event: Event) -> None:
        handler = getattr(self, f"on_{event.type}", None)
        if handler:
            handler(event.data)

    def on_phase_started(self, data: dict) -> None:
        phase = data.get("phase", "?")
        # No [skill_name] prefix — renderer prepends it from meta provenance.
        source_run_id = data.get("run_id")
        # Issue #214: when the event carries plan_step, emit a one-shot
        # "plan N/M" detail line for this run_id BEFORE the phase trace.
        # The SkillActivityRow detail is replaced on the NEXT in-phase
        # signal (on_llm_called / on_act_executed) — so the user sees
        # "plan N/M" briefly on row mount, then it gets overwritten by
        # real-time signals. That's the right tradeoff: plan context is
        # most useful at row mount; once the skill is grinding, the
        # in-phase signal carries more information.
        plan_step = data.get("plan_step")
        if (
            plan_step
            and source_run_id
            and source_run_id not in self._plan_step_announced
        ):
            n_done = plan_step.get("n_done")
            n_total = plan_step.get("n_total")
            if n_done and n_total:
                self._enqueue(
                    f"detail: plan {n_done}/{n_total}",
                    source_run_id=source_run_id,
                )
                self._plan_step_announced.add(source_run_id)
        self._enqueue(f"phase started: {phase}", source_run_id=source_run_id)

    def on_phase_completed(self, data: dict) -> None:
        phase = data.get("phase", "?")
        nxt = data.get("next", "?")
        conf = data.get("confidence")
        suffix = f"  (confidence={conf})" if conf is not None else ""
        self._enqueue(
            f"{phase} → {nxt}{suffix}",
            source_run_id=data.get("run_id"),
        )

    # ── Workflow terminal events (FP-0011 / FP-0012) ─────────────────────
    # FP-0011 removed skill_narrator and the `skill_done` outbox kind that
    # the TUI used to stop SkillActivityRow spinners. We bridge the gap
    # here: workflow_finished / workflow_aborted fire at the OS level and
    # propagate to all EventLog subscribers (including this forwarder).
    # The TUI detects the "skill done: …" prefix in its trace handler to
    # call finish_skill_row without changing the outbox contract.

    def on_workflow_finished(self, data: dict) -> None:
        self._enqueue("skill done: finished", source_run_id=data.get("run_id"))

    def on_workflow_aborted(self, data: dict) -> None:
        # C-F2 (wave-8): encode the abort reason into the trace text so
        # ``SkillActivityRow``'s ``✗`` finish line can surface *why* the
        # skill failed without the user having to switch to the events
        # tab. Forwarded as ``"skill done: aborted: <reason>"``; the
        # bare ``"skill done: aborted"`` form (= no reason field) stays
        # supported for backward-compat in case any consumer relies on
        # the old format.
        reason = str(data.get("reason") or "").strip()
        text = f"skill done: aborted: {reason}" if reason else "skill done: aborted"
        self._enqueue(text, source_run_id=data.get("run_id"))

    # ── In-phase detail signals (skill internal progress) ────────────────────
    # Without these, the SkillActivityRow showed only the phase name
    # during long LLM calls or heavy Control IR runs — a 10–30 s blank
    # window where the user couldn't tell "still working" from "stuck".
    # The ``detail: ...`` prefix is consumed by the TUI's trace handler
    # and routed to ``ConversationView.update_skill_detail`` which
    # appends a dim ``⤷ <text>`` segment to the row. Cleared by the
    # next ``phase_started``.

    def on_llm_called(self, data: dict) -> None:
        """LLM call started — surface the model name as detail.

        Fires once per LLM call; the row shows ``⤷ llm: <model>`` until
        the response arrives or the phase advances.
        """
        model = data.get("model") or "?"
        self._enqueue(f"detail: llm: {model}", source_run_id=data.get("run_id"))

    def on_llm_response_received(self, data: dict) -> None:
        """LLM call finished — clear the detail (= we're between calls).

        Without this the row would keep showing ``⤷ llm: <model>`` long
        after the response arrived, misleading users about whether the
        model is still working.
        """
        self._enqueue("detail: ", source_run_id=data.get("run_id"))

    def on_mcp_progress(self, data: dict) -> None:
        """MCP tool emitted a progress notification — surface in sticky status.

        issue #264: MCP servers can stream ``notifications/progress`` while
        a tool call is in flight. Pre-#264 these were silently dropped
        because the Reyn client didn't pass a ``progress_callback`` to the
        SDK. Now the op handler wires the callback, emits ``mcp_progress``
        events, and this forwarder converts each event into a
        ``OutboxMessage(kind="status")`` with ``meta.source="mcp"`` so the
        TUI sticky bar shows "what is the MCP server doing right now".

        Per the issue #264 owner decision (lead-coder + tui-coder alignment):
        ``kind="status"`` is the canonical surface for "long-running external
        operation visibility"; the ``meta.source`` discriminator lets future
        per-source styling decisions land without changing the kind.

        The β fallback path (= route through ``set_detail`` via
        ``kind="trace"``) remains a future option if dogfood observes
        sticky overwrite issues between MCP progress and LLM "thinking"
        — empirical fallback path documented on issue #264.
        """
        server = data.get("server") or "?"
        tool = data.get("tool") or "?"
        progress = data.get("progress")
        total = data.get("total")
        message = data.get("message")

        text = self._format_mcp_progress(server, tool, progress, total, message)

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
        source_run_id = data.get("run_id")
        if source_run_id:
            meta["run_id"] = source_run_id
            meta["run_id_short"] = source_run_id[-4:]

        try:
            self.outbox.put_nowait(
                OutboxMessage(kind="status", text=text, meta=meta),
            )
        except asyncio.QueueFull:
            pass

    @staticmethod
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
          - progress numeric, total absent / zero → raw progress value
          - neither → bare ``[mcp/<server>] <tool>`` indicator
          - message present → appended as ``· <message>``

        Pure formatter so tests can pin the rendering without mounting a forwarder.
        """
        head = f"[mcp/{server}] {tool}"
        body = ""
        try:
            prog_f = float(progress) if progress is not None else None
        except (TypeError, ValueError):
            prog_f = None
        try:
            tot_f = float(total) if total is not None else None
        except (TypeError, ValueError):
            tot_f = None
        if prog_f is not None and tot_f is not None and tot_f > 0:
            pct = (prog_f / tot_f) * 100
            body = f" · {pct:.0f}%"
        elif prog_f is not None:
            # Indeterminate total — show raw progress value as-is.
            body = f" · progress={prog_f:g}"
        text = head + body
        if message:
            text += f" · {message}"
        return text

    def on_act_executed(self, data: dict) -> None:
        """Control IR ops just ran — show a short summary as detail.

        ``act_executed`` fires after a batch of ops complete; the
        ``op_count`` from the event payload is the count of ops in that
        batch. Useful as a "the skill is actively working" signal during
        heavy preprocessor turns.

        Issue #161 finding #3: when ``op_kinds`` is present, surface the
        distinct kinds in parentheses so the user can tell "parent ran
        a write_file batch" vs "parent spawned a sub-skill". Without
        this distinction every ``act: N ops`` looked identical.
        """
        op_count = data.get("op_count")
        if not op_count:
            return
        op_kinds = data.get("op_kinds") or []
        # De-duplicate while preserving first-occurrence order so the
        # detail reads naturally (e.g. ``run_skill, write_file`` rather
        # than ``run_skill, run_skill, write_file``). Bounded to the
        # first 3 distinct kinds with a tail ellipsis so the line stays
        # short even on large batches.
        seen: list[str] = []
        for k in op_kinds:
            if k and k not in seen:
                seen.append(k)
            if len(seen) >= 4:
                break
        kinds_suffix = ""
        if seen:
            display = seen[:3]
            tail = "…" if len(seen) > 3 else ""
            kinds_suffix = f" ({', '.join(display)}{tail})"
        self._enqueue(
            f"detail: act: {op_count} op{'s' if op_count != 1 else ''}"
            f"{kinds_suffix}",
            source_run_id=data.get("run_id"),
        )

    # NOTE: tool-call lifecycle handlers (on_tool_called / on_tool_returned /
    # on_tool_failed) live on ``ChatLifecycleForwarder`` instead — those
    # events fire from ``dispatch/dispatcher.py`` against the session-level
    # event log, not the per-skill event log this forwarder subscribes to.
    # See ``feedback_verify_existing_event_emission_before_adding`` for the
    # subscriber-layer lesson (wave-#427 smoke finding 2026-05-22).

    def _enqueue(self, text: str, *, source_run_id: str | None = None) -> None:
        # Fire-and-forget: trace messages are advisory, never block the skill.
        #
        # Issue #134 — sub-skill attribution: prefer the event's own
        # ``run_id`` (= the skill run that actually emitted the event)
        # over ``self.run_id`` (= the run that constructed this
        # forwarder).  When the two differ, stamp ``parent_run_id`` so
        # TUI consumers can render nested rows.  Falls back to the
        # forwarder's own run_id when the event carries none (= pre-
        # issue-#134 emit sites + non-runtime events).
        effective_run_id = source_run_id or self.run_id
        meta: dict = {"skill_name": self.skill_name}
        if effective_run_id:
            meta["run_id"] = effective_run_id
            meta["run_id_short"] = effective_run_id[-4:]
        if (
            source_run_id is not None
            and self.run_id is not None
            and source_run_id != self.run_id
        ):
            meta["parent_run_id"] = self.run_id
        try:
            self.outbox.put_nowait(OutboxMessage(kind="trace", text=text, meta=meta))
        except asyncio.QueueFull:
            pass
