"""Restore-on-restart projection: persisted ``ChatMessage`` log → display frames.

Phase 5 of the TUI-rebuild arc (#3273). On ``reyn chat`` restart the Textual
conversation pane should show the PREVIOUS conversation (Claude-Code ``--resume``
parity) rather than starting blank. :func:`project_restored_frames` is the pure
projection that turns the persisted conversation log — a ``list[ChatMessage]``
loaded from ``history.jsonl`` (``Session.load_history``) — into the
:class:`~reyn.runtime.outbox.OutboxMessage` display frames the app's retained
``FlowModel`` is fed from, so a restored turn renders through the EXACT SAME
presenter/gutter path a live frame does (only the timing differs: these are
appended once at ``on_mount`` BEFORE the live frame pump starts).

**Source of truth (D1, architect-ratified).** The restore source is the
persisted ``ChatMessage`` log (``history.jsonl``), NOT the P6 audit-event log:
audit-events do not carry assistant text / tool results and rotate. This
projection reads ONLY ``ChatMessage`` instances (see the accessor
:meth:`reyn.interfaces.repl.read_model.ChatReadModel.conversation_history`).

**Non-authoritative projection (recovery truncate-falsify gate is N/A).** The
``FlowModel`` this feeds is a *projection*; the authority is
``history.jsonl`` / the ``ChatMessage`` log itself. This reads that authoritative
store DIRECTLY — it is derived-at-read, NOT WAL-event-reconstructed state — so
the CLAUDE.md recovery-feature truncate-falsify gate (which guards
WAL-event-derived reconstruction state against WAL truncation below its source
events) does not apply here: there is no WAL-derived state to lose. If a future
change makes any restored surface WAL-reconstructed rather than read straight
from the ChatMessage log, that gate WOULD apply and this note must be revisited.

**Resolved, never RUNNING.** A restored tool result is a SETTLED outcome, so it
projects to a ``tool_call_completed`` frame; the caller (``_hydrate_from_history``)
gives it the SUCCESS/ERROR lifecycle state the live path's completion handler
would have, derived from the SAME ``summarize_tool_result`` the presenter reads —
so gutter colour and body agree. The transient ``tool_call_started`` progress
header (a live-pump affordance with no meaning in a static restore) is
intentionally omitted, and the ``system`` / ``summary`` roles are Reyn-internal
chrome (filtered at the LLM wire boundary), so both are skipped.

This module imports only :class:`~reyn.runtime.outbox.OutboxMessage` (no
``textual`` / ``textual_flowview``): the projection is pure and unit-testable
without mounting the app.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.runtime.chat_message import ChatMessage
    from reyn.runtime.outbox import OutboxMessage

# Roles with no operator-facing conversation surface: ``system`` is persisted
# lifecycle chrome and ``summary`` is the chat-compactor's internal carry —
# both filtered at the LLM wire boundary, so neither belongs in the restored
# scrollback.
_SKIP_ROLES = frozenset({"system", "summary"})

#: Marker stamped on every restored frame's ``meta`` so a consumer (or a test)
#: can tell a hydrated turn from a live one. Purely informational — the frame
#: renders identically either way.
RESTORED_META_KEY = "_restored"

#: Leading divider row announcing that what follows is the resumed prior
#: conversation (operator legibility — the Product-Think lens). Emitted once,
#: only when there is at least one restored turn.
_RESUME_DIVIDER = "⤺ resumed previous conversation"


def project_restored_frames(
    messages: "list[ChatMessage]",
) -> "list[OutboxMessage]":
    """Project a persisted ``ChatMessage`` log into restore display frames.

    Oldest→newest, preserving conversation order. Mapping:

    - ``user`` (non-empty text)      → ``kind="user"``
    - ``assistant`` (non-empty text) → ``kind="agent"``
    - ``tool`` (a tool RESULT)       → ``kind="tool_call_completed"`` carrying
      ``meta["tool"]`` / ``meta["result"]`` for the presenter's result summary
    - ``system`` / ``summary``       → skipped (internal chrome)

    A ``tool_call_started`` header is NOT emitted (a live-progress affordance
    with no meaning in a static restore). Every frame is stamped
    ``meta[RESTORED_META_KEY]=True``. Returns ``[]`` for an empty log (no
    divider), so a first-ever run stays blank.
    """
    from reyn.runtime.outbox import (
        OutboxMessage,  # noqa: PLC0415 — keep module textual-free at import
    )

    frames: "list[OutboxMessage]" = []
    for m in messages:
        role = m.role
        if role in _SKIP_ROLES:
            continue
        if role == "user":
            text = m.text
            if text.strip():
                frames.append(
                    OutboxMessage(kind="user", text=text, meta={RESTORED_META_KEY: True})
                )
        elif role == "assistant":
            text = m.text
            if text.strip():
                frames.append(
                    OutboxMessage(kind="agent", text=text, meta={RESTORED_META_KEY: True})
                )
        elif role == "tool":
            frames.append(
                OutboxMessage(
                    kind="tool_call_completed",
                    text=m.name or "",
                    meta={
                        RESTORED_META_KEY: True,
                        "tool": m.name,
                        "result": m.text,
                    },
                )
            )
    if not frames:
        return frames
    divider = OutboxMessage(
        kind="system", text=_RESUME_DIVIDER, meta={RESTORED_META_KEY: True}
    )
    return [divider, *frames]


__all__ = ["RESTORED_META_KEY", "project_restored_frames"]
