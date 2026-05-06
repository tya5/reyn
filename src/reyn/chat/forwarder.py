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
