"""OutboxMessage → Chainlit payload mapper.

Pure conversion layer with no chainlit dependency. Unit tests can
exercise this without installing the ``[chainlit]`` extra.

Kind coverage:
- ``agent``                → main reply, author "agent",
                              type "assistant_message"
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
- ``presentation``         → author "📊 present", type "system_message";
                              the ``present`` op render model in ``meta["nodes"]``
                              serialized to Markdown (#2708 P1 — the #2688 fix; the
                              generic fall-through dropped it as empty ``text``)
- ``system``               → author "ℹ system", type "system_message"
- ``__stream_*__``         → dropped (TUI-only incremental render kinds)
- ``__end__``              → sentinel, signals drain to stop (returns None)
- anything else            → author "ℹ system", type "system_message"

The author labels carry visible emoji prefixes so the chainlit UI
renders distinct avatars + glyphs for the tool / system / status
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

from reyn.runtime.outbox import OutboxMessage

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
    "intervention": "❓ intervention",
    "system": "ℹ system",
}

_AUTHOR_TOOL = "🔧 tool"
_AUTHOR_FALLBACK = "ℹ system"


# #1642: char caps (NOT byte — multibyte/JA-safe; Python str slicing is char-based)
# so a large arg/result doesn't fill the conversation (full content out of scope for
# this inline row). args ~120 / result ~200 per lead; cross-surface-aligned with the
# TUI renderer. _TOOL_VALUE_LIMIT caps one arg value so a single
# big arg can't dominate the whole-args preview.
_TOOL_ARGS_LIMIT = 120
_TOOL_RESULT_LIMIT = 200
_TOOL_VALUE_LIMIT = 80


def _truncate_one_line(s: str, limit: int) -> str:
    """Collapse to one line + cap CHAR length with an ellipsis (multibyte-safe)."""
    s = " ".join(s.split())  # collapse whitespace/newlines to a single line
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _preview_tool_content(value: object, limit: int) -> str:
    """#1642: a one-line, length-bounded preview of a tool call's args dict or
    result, for inline display in the conversation row. Args render as a compact
    ``key=value, …``; a scalar/other result renders via str/repr. Empty/None ⇒ ``""``
    (caller falls back to the bare name — no noisy ``name()``)."""
    if value is None or value == {} or value == "":
        return ""
    if isinstance(value, dict):
        body = ", ".join(
            f"{k}={_truncate_one_line(v if isinstance(v, str) else repr(v), _TOOL_VALUE_LIMIT)}"
            for k, v in value.items()
        )
    else:
        body = value if isinstance(value, str) else repr(value)
    return _truncate_one_line(body, limit)


def _format_tool_call(msg: OutboxMessage) -> str:
    """Build the tool-call body text with a status-prefix marker.

    The lifecycle forwarder packs the tool name into both ``text`` and
    ``meta["tool"]`` so either works; we prefer meta when present so a
    future emitter that uses a richer text field won't collide with the prefix.
    #1642: args (``meta["args"]``) render inline on start and result
    (``meta["result"]``) as a preview on completion — both truncated.
    """
    meta = msg.meta if isinstance(msg.meta, dict) else {}
    name = meta.get("tool")
    if not isinstance(name, str) or not name:
        name = msg.text or "(unnamed)"
    if msg.kind == "tool_call_started":
        args = _preview_tool_content(meta.get("args"), _TOOL_ARGS_LIMIT)
        return f"→ {name}({args})" if args else f"→ {name}"
    if msg.kind == "tool_call_completed":
        result = _preview_tool_content(meta.get("result"), _TOOL_RESULT_LIMIT)
        return f"✓ {name} → {result}" if result else f"✓ {name}"
    # tool_call_failed
    em = meta.get("error_message")
    err = f": {em}" if isinstance(em, str) and em else ""
    return f"✗ {name}{err}"


_AUTHOR_PRESENT = "📊 present"


def _present_node_to_markdown(node: dict) -> str:
    """Render ONE `ResolvedPresentation` node (the FP-0054 render model — see
    `interfaces/repl/present_renderer.py` for the terminal twin) to a Markdown
    fragment for the chainlit browser UI. The render model is already bound /
    neutralized / capped upstream; this is a display-only serialization."""
    component = node.get("component")
    text = node.get("text", "")
    if component in ("text", "markdown"):
        return text
    if component == "code":
        lang = node.get("language") or ""
        return f"```{lang}\n{text}\n```"
    if component == "diff":
        return f"```diff\n{text}\n```"
    if component == "keyvalue":
        return "\n".join(
            f"**{row.get('label', '')}**: {row.get('value', '')}"
            for row in node.get("rows", [])
        )
    if component == "list":
        return "\n".join(f"- {item}" for item in node.get("items", []))
    if component == "table":
        columns = node.get("columns", [])
        if not columns:
            return ""
        headers = [str(c.get("header", "")) for c in columns]
        n_rows = max((len(c.get("cells", [])) for c in columns), default=0)
        lines = ["| " + " | ".join(headers) + " |",
                 "| " + " | ".join("---" for _ in headers) + " |"]
        for i in range(n_rows):
            cells = [
                str(c["cells"][i]) if i < len(c.get("cells", [])) else ""
                for c in columns
            ]
            lines.append("| " + " | ".join(cells) + " |")
        return "\n".join(lines)
    if component == "image":
        alt = node.get("alt") or node.get("src") or ""
        return f"_[image: {alt}]_"
    # Unregistered/future component — never crash the drain over one bad node.
    return f"_<unsupported present component {component!r}>_"


def _presentation_nodes_to_markdown(nodes: list) -> str:
    """Serialize a `ResolvedPresentation.nodes` render model to a single Markdown
    block for chainlit. #2708 P1: this is the chainlit side of the forced #2688
    present fix — before it, kind="presentation" fell through to the generic branch,
    which used the (empty) `text` field and dropped the render model in `meta["nodes"]`,
    so a `present` op was invisible in chainlit despite ok:True."""
    return "\n\n".join(
        _present_node_to_markdown(n) for n in nodes if isinstance(n, dict)
    )


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

    # #2708 P1: a `present` op's render model rides on ``meta["nodes"]`` (the outbox
    # message's ``text`` is ""), so it MUST be rendered from nodes — the generic
    # fall-through below would emit empty content (the #2688 chainlit silent-drop bug).
    if kind == "presentation":
        meta = msg.meta if isinstance(msg.meta, dict) else {}
        return ChainlitPayload(
            role="message",
            author=_AUTHOR_PRESENT,
            content=_presentation_nodes_to_markdown(meta.get("nodes", [])),
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
