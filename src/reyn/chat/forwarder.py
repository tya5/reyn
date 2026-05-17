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

    def __call__(self, event: Event) -> None:
        handler = getattr(self, f"on_{event.type}", None)
        if handler:
            handler(event.data)

    def on_phase_started(self, data: dict) -> None:
        phase = data.get("phase", "?")
        # No [skill_name] prefix — renderer prepends it from meta provenance.
        self._enqueue(f"phase started: {phase}")

    def on_phase_completed(self, data: dict) -> None:
        phase = data.get("phase", "?")
        nxt = data.get("next", "?")
        conf = data.get("confidence")
        suffix = f"  (confidence={conf})" if conf is not None else ""
        self._enqueue(f"{phase} → {nxt}{suffix}")

    # ── Workflow terminal events (FP-0011 / FP-0012) ─────────────────────
    # FP-0011 removed skill_narrator and the `skill_done` outbox kind that
    # the TUI used to stop SkillActivityRow spinners. We bridge the gap
    # here: workflow_finished / workflow_aborted fire at the OS level and
    # propagate to all EventLog subscribers (including this forwarder).
    # The TUI detects the "skill done: …" prefix in its trace handler to
    # call finish_skill_row without changing the outbox contract.

    def on_workflow_finished(self, data: dict) -> None:
        self._enqueue("skill done: finished")

    def on_workflow_aborted(self, data: dict) -> None:
        self._enqueue("skill done: aborted")

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
        self._enqueue(f"detail: llm: {model}")

    def on_llm_response_received(self, data: dict) -> None:
        """LLM call finished — clear the detail (= we're between calls).

        Without this the row would keep showing ``⤷ llm: <model>`` long
        after the response arrived, misleading users about whether the
        model is still working.
        """
        self._enqueue("detail: ")

    def on_act_executed(self, data: dict) -> None:
        """Control IR ops just ran — show a short summary as detail.

        ``act_executed`` fires after a batch of ops complete; the
        ``op_count`` from the event payload is the count of ops in that
        batch. Useful as a "the skill is actively working" signal during
        heavy preprocessor turns.
        """
        op_count = data.get("op_count")
        if op_count:
            self._enqueue(f"detail: act: {op_count} op{'s' if op_count != 1 else ''}")

    def _enqueue(self, text: str) -> None:
        # Fire-and-forget: trace messages are advisory, never block the skill.
        meta: dict = {"skill_name": self.skill_name}
        if self.run_id:
            meta["run_id"] = self.run_id
            meta["run_id_short"] = self.run_id[-4:]
        try:
            self.outbox.put_nowait(OutboxMessage(kind="trace", text=text, meta=meta))
        except asyncio.QueueFull:
            pass
