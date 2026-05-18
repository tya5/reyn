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


__all__ = ["ChatLifecycleForwarder"]
