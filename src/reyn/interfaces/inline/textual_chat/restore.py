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

**Coalesced, resolved, never RUNNING.** A restored tool result is a SETTLED
outcome and is projected into the SAME coalesced shape the LIVE path settles a
completed tool into (``app.py``'s ``_coalesce_tool_result``): a single
``kind="tool_call_started"`` frame whose ``meta`` carries both the ORIGINAL
tool call (``tool`` / ``args``, correlated from the preceding assistant
message's ``tool_calls`` by ``tool_call_id``) and the result, stashed under
``RESULT_KIND_KEY`` / ``RESULT_META_KEY`` (:mod:`._meta_keys`). The presenter's
existing ``tool_call_started`` branch renders this as ONE block —
``⏺ tool(args)`` + ``⎿ result`` — with no presenter change needed, so a
restored tool turn is visually identical to a live settled one (no orphan
``⎿``-only row). An uncorrelated result (no matching ``tool_call_id`` — e.g.
truncated history) still projects to the same coalesced shape with just the
tool name as header (empty args) — it never regresses to an orphan row. The
caller (``_hydrate_from_history``) gives the entry the SUCCESS/ERROR lifecycle
state the live path's completion handler would have, derived from the SAME
``summarize_tool_result`` the presenter reads, so gutter colour and body
agree. The ``system`` / ``summary`` roles are Reyn-internal chrome (filtered
at the LLM wire boundary), so both are skipped.

This module imports only :class:`~reyn.runtime.outbox.OutboxMessage` and the
plain ``str`` constants in :mod:`._meta_keys` (no ``textual`` /
``textual_flowview``): the projection is pure and unit-testable without
mounting the app.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ._meta_keys import RESULT_KIND_KEY, RESULT_META_KEY

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


def _correlated_calls(messages: "list[ChatMessage]") -> "dict[str, dict]":
    """Map ``tool_call_id -> {"name": ..., "args": ...}`` from assistant turns.

    Every ``role=="assistant"`` message's ``.tool_calls`` (a
    ``list[dict]`` of ``{"id": <tool_call_id>, "function": {"name": ...,
    "arguments": <json str>}}``) contributes one entry per call. ``arguments``
    is parsed defensively — a malformed / non-JSON string falls back to the
    raw string, never raises — so a corrupt history entry degrades to a
    plain-text arg summary rather than crashing the projection.
    """
    calls: "dict[str, dict]" = {}
    for m in messages:
        if m.role != "assistant" or not m.tool_calls:
            continue
        for call in m.tool_calls:
            call_id = call.get("id")
            if not call_id:
                continue
            fn = call.get("function") or {}
            name = fn.get("name")
            raw_args = fn.get("arguments")
            args: object = {}
            if isinstance(raw_args, str) and raw_args:
                try:
                    args = json.loads(raw_args)
                except (ValueError, TypeError):
                    args = raw_args
            elif raw_args is not None:
                args = raw_args
            calls[call_id] = {"name": name, "args": args}
    return calls


def project_restored_frames(
    messages: "list[ChatMessage]",
) -> "list[OutboxMessage]":
    """Project a persisted ``ChatMessage`` log into restore display frames.

    Oldest→newest, preserving conversation order. Mapping:

    - ``user`` (non-empty text)      → ``kind="user"``
    - ``assistant`` (non-empty text) → ``kind="agent"``
    - ``tool`` (a tool RESULT)       → a SINGLE ``kind="tool_call_started"``
      frame carrying the coalesced call+result shape (see the module
      docstring): ``meta["tool"]`` / ``meta["args"]`` (correlated from the
      preceding assistant message's ``tool_calls`` by ``tool_call_id``,
      falling back to ``m.name`` / empty args when uncorrelated) plus
      ``RESULT_KIND_KEY="tool_call_completed"`` and
      ``RESULT_META_KEY={"result": m.text}``.
    - ``system`` / ``summary``       → skipped (internal chrome)

    An assistant message carrying ONLY ``tool_calls`` (empty text) produces no
    ``agent`` frame of its own — the call header is delivered entirely through
    the coalesced ``tool`` projection above, matching how the live coalesce
    path folds a call into its result rather than emitting two rows.

    Every frame is stamped ``meta[RESTORED_META_KEY]=True``. Returns ``[]``
    for an empty log (no divider), so a first-ever run stays blank.
    """
    from reyn.runtime.outbox import (
        OutboxMessage,  # noqa: PLC0415 — keep module textual-free at import
    )

    calls_by_id = _correlated_calls(messages)
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
            call = calls_by_id.get(m.tool_call_id or "", {})
            tool_name = call.get("name") or m.name
            frames.append(
                OutboxMessage(
                    kind="tool_call_started",
                    text=tool_name or "",
                    meta={
                        RESTORED_META_KEY: True,
                        "tool": tool_name,
                        "args": call.get("args") or {},
                        RESULT_KIND_KEY: "tool_call_completed",
                        RESULT_META_KEY: {"result": m.text},
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
