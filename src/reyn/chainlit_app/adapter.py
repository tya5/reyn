"""OutboxMessage → Chainlit payload mapper.

Pure conversion layer with no chainlit dependency. Unit tests can
exercise this without installing the ``[chainlit]`` extra.

Kind coverage:
- ``agent``                → main reply, author "agent",
                              type "assistant_message"
- ``skill_done``           → author "✨ skill", type "system_message"
- ``status``               → author "⚙ status", type "system_message"
- ``error``                → red note via ``cl.ErrorMessage``
- ``intervention``         → author "❓ intervention" (full handler in
                              app._handle_intervention; this branch
                              only fires when the iv lookup fails)
- ``tool_call_started``    → author "🔧 tool", type "system_message",
                              text "→ <name>"
- ``tool_call_completed``  → author "🔧 tool", type "system_message",
                              text "✓ <name>"
- ``tool_call_failed``     → author "🔧 tool", type "system_message",
                              text "✗ <name>: <err>"
- ``trace``                → dropped (debug noise)
- ``system``               → author "ℹ system", type "system_message"
                              (plan_summary / plan_complete /
                              plan_aborted bridge messages)
- ``__stream_*__``         → dropped (TUI-only incremental render kinds)
- ``__end__``              → sentinel, signals drain to stop (returns None)
- anything else            → author "ℹ system", type "system_message"

The author labels carry visible emoji prefixes so the chainlit UI
renders distinct avatars + glyphs for the tool / system / skill
streams — without the prefix every author got the same generic
auto-avatar and tool rows looked indistinguishable from assistant
prose (user dogfood 2026-05-25 observation).

``type="system_message"`` on the non-agent kinds also engages
chainlit's secondary message styling (smaller / dimmer bubble), so
tool / status frames sit visually behind the assistant's reply
rather than competing with it.
"""
from __future__ import annotations

from dataclasses import dataclass

from reyn.chat.outbox import OutboxMessage

# Chainlit ``cl.Message.type`` literals we use. Reference:
# .venv/lib/.../chainlit/message.py ``MessageStepType``.
MSG_TYPE_ASSISTANT = "assistant_message"
MSG_TYPE_SYSTEM = "system_message"


@dataclass(frozen=True)
class ChainlitPayload:
    """What the chainlit drain loop should send to the browser.

    ``role`` selects the cl-side renderer branch:
    - ``"message"``: ``cl.Message(author=..., content=..., type=...).send()``
    - ``"error"``: ``cl.ErrorMessage(content=content).send()``
    - ``"end"``: drain loop terminates (no send)

    Drain loop callers should treat ``None`` (= drop) and ``role="end"``
    as separate signals: ``None`` skip-this-frame, ``end`` exit-loop.

    ``message_type`` is the chainlit ``cl.Message.type`` literal.
    Non-agent kinds use ``"system_message"`` so the UI styles them as
    secondary frames (= smaller bubble, dim avatar) rather than
    competing with the assistant's prose reply at the same visual
    weight.
    """
    role: str
    author: str
    content: str
    message_type: str = MSG_TYPE_ASSISTANT


_TOOL_KINDS = frozenset({
    "tool_call_started", "tool_call_completed", "tool_call_failed",
})

# Author label includes a visible emoji prefix so chainlit's
# auto-generated avatar (= derived from the first character of
# ``author``) lands a distinct glyph + colour per stream. Without
# the prefix every author got a similar generic avatar and operators
# couldn't visually distinguish tool / status / agent rows at a glance.
_AUTHOR_BY_KIND: dict[str, str] = {
    "agent": "agent",
    "status": "⚙ status",
    "skill_done": "✨ skill",
    "intervention": "❓ intervention",
    "system": "ℹ system",
}

_AUTHOR_TOOL = "🔧 tool"
_AUTHOR_FALLBACK = "ℹ system"


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
    """Map one OutboxMessage to a Chainlit payload, or None to drop."""
    kind = msg.kind
    text = msg.text or ""

    if kind == "__end__":
        return ChainlitPayload(
            role="end", author="", content="",
            message_type=MSG_TYPE_ASSISTANT,
        )

    if kind.startswith("__stream_") or kind == "trace":
        return None

    if kind == "error":
        return ChainlitPayload(
            role="error", author="error", content=text,
            message_type=MSG_TYPE_SYSTEM,
        )

    if kind in _TOOL_KINDS:
        return ChainlitPayload(
            role="message",
            author=_AUTHOR_TOOL,
            content=_format_tool_call(msg),
            message_type=MSG_TYPE_SYSTEM,
        )

    author = _AUTHOR_BY_KIND.get(kind, _AUTHOR_FALLBACK)
    # Only the assistant's prose reply uses assistant_message; every
    # other kind sits visually behind it via system_message.
    msg_type = MSG_TYPE_ASSISTANT if kind == "agent" else MSG_TYPE_SYSTEM
    return ChainlitPayload(
        role="message", author=author, content=text, message_type=msg_type,
    )


__all__ = [
    "MSG_TYPE_ASSISTANT",
    "MSG_TYPE_SYSTEM",
    "ChainlitPayload",
    "outbox_to_chainlit",
]
