"""OutboxMessage → Chainlit payload mapper.

Pure conversion layer with no chainlit dependency. Unit tests can
exercise this without installing the ``[chainlit]`` extra.

PoC kind coverage:
- ``agent``        → main reply, author "agent"
- ``skill_done``   → green-bordered system note, author "skill"
- ``status``       → dim hint, author "status"
- ``error``        → red note, author "error"
- ``intervention`` → author "intervention" (full handler is V2 — for
                     PoC just render as text)
- ``trace``        → dropped (debug noise)
- ``system``       → author "system" (plan_summary / plan_complete /
                     plan_aborted bridge messages)
- ``__stream_*__`` → dropped (TUI-only incremental render kinds)
- ``__end__``      → sentinel, signals drain to stop (returns None)
- anything else    → author "system", text passed through
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


def outbox_to_chainlit(msg: OutboxMessage) -> ChainlitPayload | None:
    """Map one OutboxMessage to a Chainlit payload, or None to drop.

    Returns:
        - ``ChainlitPayload(role="end", ...)`` for ``kind="__end__"``.
        - ``None`` for kinds the PoC intentionally drops
          (``trace``, ``__stream_*__`` incremental render frames).
        - ``ChainlitPayload(role="error", ...)`` for ``kind="error"``.
        - ``ChainlitPayload(role="message", author=<label>, content=<text>)``
          for everything else.
    """
    kind = msg.kind
    text = msg.text or ""

    if kind == "__end__":
        return ChainlitPayload(role="end", author="", content="")

    if kind.startswith("__stream_") or kind == "trace":
        return None

    if kind == "error":
        return ChainlitPayload(role="error", author="error", content=text)

    author = {
        "agent": "agent",
        "status": "status",
        "skill_done": "skill",
        "intervention": "intervention",
        "system": "system",
    }.get(kind, "system")

    return ChainlitPayload(role="message", author=author, content=text)


__all__ = ["ChainlitPayload", "outbox_to_chainlit"]
