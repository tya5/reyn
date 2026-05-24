"""OutboxMessage → Chainlit payload mapper.

Pure conversion layer with no chainlit dependency. Unit tests can
exercise this without installing the ``[chainlit]`` extra.

Kind coverage:
- ``agent``                → main reply, author "agent"
- ``skill_done``           → green-bordered system note, author "skill"
- ``status``               → dim hint, author "status"
- ``error``                → red note, author "error"
- ``intervention``         → author "intervention" (full handler is V2 — for
                             PoC just render as text)
- ``tool_call_started``    → author "tool", text "→ <name>" (= visual
                             start marker, distinct from agent / system)
- ``tool_call_completed``  → author "tool", text "✓ <name>"
- ``tool_call_failed``     → author "tool", text "✗ <name>: <err>"
- ``trace``                → dropped (debug noise)
- ``system``               → author "system" (plan_summary / plan_complete /
                             plan_aborted bridge messages)
- ``__stream_*__``         → dropped (TUI-only incremental render kinds)
- ``__end__``              → sentinel, signals drain to stop (returns None)
- anything else            → author "system", text passed through
"""
from __future__ import annotations

from dataclasses import dataclass

from reyn.chat.outbox import OutboxMessage


@dataclass(frozen=True)
class ChainlitPayload:
    """What the chainlit drain loop should send to the browser.

    ``role`` selects the cl-side renderer branch:
    - ``"message"``: ``cl.Message(author=author, content=content).send()``
    - ``"error"``: ``cl.ErrorMessage(content=content).send()``
    - ``"end"``: drain loop terminates (no send)

    Drain loop callers should treat ``None`` (= drop) and ``role="end"``
    as separate signals: ``None`` skip-this-frame, ``end`` exit-loop.
    """
    role: str
    author: str
    content: str


_TOOL_KINDS = frozenset({
    "tool_call_started", "tool_call_completed", "tool_call_failed",
})


def _format_tool_call(msg: OutboxMessage) -> str:
    """Build the tool-call body text with a status-prefix marker.

    The lifecycle forwarder packs the tool name into both ``text`` and
    ``meta["tool"]`` so either works; we prefer meta when present so a
    future emitter that uses a richer text field (e.g. with args
    inline) won't collide with the prefix.
    """
    name = msg.meta.get("tool") if isinstance(msg.meta, dict) else None
    if not isinstance(name, str) or not name:
        name = msg.text or "(unnamed)"
    if msg.kind == "tool_call_started":
        return f"→ {name}"
    if msg.kind == "tool_call_completed":
        return f"✓ {name}"
    # tool_call_failed
    err = ""
    if isinstance(msg.meta, dict):
        em = msg.meta.get("error_message")
        if isinstance(em, str) and em:
            err = f": {em}"
    return f"✗ {name}{err}"


def outbox_to_chainlit(msg: OutboxMessage) -> ChainlitPayload | None:
    """Map one OutboxMessage to a Chainlit payload, or None to drop.

    Returns:
        - ``ChainlitPayload(role="end", ...)`` for ``kind="__end__"``.
        - ``None`` for kinds the PoC intentionally drops
          (``trace``, ``__stream_*__`` incremental render frames).
        - ``ChainlitPayload(role="error", ...)`` for ``kind="error"``.
        - ``ChainlitPayload(role="message", author=<label>, content=<text>)``
          for everything else (including the 3 ``tool_call_*`` kinds,
          which use author ``"tool"`` so the chat thread visually
          separates tool activity from the agent's prose reply).
    """
    kind = msg.kind
    text = msg.text or ""

    if kind == "__end__":
        return ChainlitPayload(role="end", author="", content="")

    if kind.startswith("__stream_") or kind == "trace":
        return None

    if kind == "error":
        return ChainlitPayload(role="error", author="error", content=text)

    if kind in _TOOL_KINDS:
        return ChainlitPayload(
            role="message",
            author="tool",
            content=_format_tool_call(msg),
        )

    author = {
        "agent": "agent",
        "status": "status",
        "skill_done": "skill",
        "intervention": "intervention",
        "system": "system",
    }.get(kind, "system")

    return ChainlitPayload(role="message", author=author, content=text)


__all__ = ["ChainlitPayload", "outbox_to_chainlit"]
