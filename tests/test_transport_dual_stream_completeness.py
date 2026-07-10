"""Tier 2: the transport forwards EVERY chat-event the renderer consumes (P1).

FP-0056-isomorphic completeness gate — the structural form of the A2 dual-stream
bug ("an outbox-only wire drops WaitingOn"). It enumerates the chat-event types
the renderer's ``on_chat_event`` actually branches on — by AST-scanning the
renderer source (the equality/membership literals) UNION the ``_WAITING_ON_BY_EVENT``
tool-axis table — and asserts EACH is in the transport's forwarded set
(``renderer_chat_events``). A renderer event the transport does not forward ⇒
RED, so a future renderer event that isn't wired through the transport fails CI
instead of silently vanishing on the wire.

The enumeration reads the renderer's real code (not the transport's own
derivation), so the two are bound independently — the gate is not circular.
"""
from __future__ import annotations

import ast
from pathlib import Path

from reyn.interfaces.inline.app import _WAITING_ON_BY_EVENT
from reyn.interfaces.transport.frames import renderer_chat_events

_RENDERER = (
    Path(__file__).resolve().parents[1]
    / "src" / "reyn" / "interfaces" / "repl" / "renderer.py"
)


def _renderer_consumed_event_literals() -> set[str]:
    """Every string literal the renderer's ``on_chat_event`` methods compare
    ``etype`` against — the turn-lifecycle + intervention-answer half of the
    vocabulary (the tool-axis half is the ``_WAITING_ON_BY_EVENT`` table).

    Collecting only strings inside ``ast.Compare`` nodes excludes incidental
    literals like ``getattr(event, "type")`` — those are Call args, not compares.
    """
    tree = ast.parse(_RENDERER.read_text(encoding="utf-8"))
    consumed: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "on_chat_event"):
            continue
        for cmp_node in ast.walk(node):
            if not isinstance(cmp_node, ast.Compare):
                continue
            for lit in ast.walk(cmp_node):
                if isinstance(lit, ast.Constant) and isinstance(lit.value, str):
                    consumed.add(lit.value)
    return consumed


def test_transport_forwards_every_renderer_consumed_chat_event() -> None:
    """Tier 2: each chat-event the renderer consumes is in the transport's
    forward-set. Un-forwarded ⇒ RED (the A2 dual-stream bug, designed out)."""
    consumed = _renderer_consumed_event_literals() | set(_WAITING_ON_BY_EVENT.keys())
    forwarded = renderer_chat_events()

    # Sanity: the enumeration actually found the renderer's vocabulary (a broken
    # scan that found nothing must not vacuously pass).
    assert "turn_started" in consumed
    assert {"tool_called", "tool_returned", "tool_failed"} <= consumed

    missing = consumed - forwarded
    assert not missing, (
        "renderer consumes chat-events the transport does NOT forward — they "
        f"would vanish on the wire (A2 dual-stream bug): {sorted(missing)}"
    )
