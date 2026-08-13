"""Tier 2: the transport forwards EVERY audit-event the renderer consumes (P1).

FP-0056-isomorphic completeness gate — the structural form of the A2 dual-stream
bug ("an outbox-only wire drops WaitingOn"). It enumerates the audit-event types
the renderer's ``on_audit_event`` actually branches on — by AST-scanning the
renderer source (the equality/membership literals) UNION the ``_WAITING_ON_BY_EVENT``
tool-axis table — and asserts EACH is in the transport's forwarded set
(``forwarded_frame_kinds``). A renderer event the transport does not forward ⇒
RED, so a future renderer event that isn't wired through the transport fails CI
instead of silently vanishing on the wire.

The enumeration reads the renderer's real code (not the transport's own
derivation), so the two are bound independently — the gate is not circular.
"""
from __future__ import annotations

import ast

from reyn.interfaces.repl.status import _WAITING_ON_BY_EVENT
from reyn.interfaces.transport.frames import forwarded_frame_kinds
from tests._support.paths import REPO_ROOT

_RENDERER = (
    REPO_ROOT
    / "src" / "reyn" / "interfaces" / "repl" / "renderer.py"
)


def _renderer_consumed_event_literals() -> set[str]:
    """Every string literal the renderer's ``on_audit_event`` methods compare
    ``etype`` against — the turn-lifecycle + intervention-answer half of the
    vocabulary (the tool-axis half is the ``_WAITING_ON_BY_EVENT`` table).

    Collecting only strings inside ``ast.Compare`` nodes excludes incidental
    literals like ``getattr(event, "type")`` — those are Call args, not compares.

    UNDER-CAPTURE GUARD: this scans compares *directly inside* ``on_audit_event``.
    If a renderer moves its ``etype ==`` branch into a helper that
    ``on_audit_event`` merely calls, the literal leaves this function body and is
    silently dropped — widen the scan to follow the helper (or inline the branch)
    before that refactor lands. (ADR-0039 P2 drive-by.)
    """
    tree = ast.parse(_RENDERER.read_text(encoding="utf-8"))
    consumed: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "on_audit_event"):
            continue
        for cmp_node in ast.walk(node):
            if not isinstance(cmp_node, ast.Compare):
                continue
            for lit in ast.walk(cmp_node):
                if isinstance(lit, ast.Constant) and isinstance(lit.value, str):
                    consumed.add(lit.value)
    return consumed


def test_transport_forwards_every_renderer_consumed_audit_event() -> None:
    """Tier 2: each audit-event the renderer consumes is in the transport's
    forward-set. Un-forwarded ⇒ RED (the A2 dual-stream bug, designed out)."""
    consumed = _renderer_consumed_event_literals() | set(_WAITING_ON_BY_EVENT.keys())
    forwarded = forwarded_frame_kinds()

    # Sanity: the enumeration actually found the renderer's vocabulary (a broken
    # scan that found nothing must not vacuously pass).
    assert "turn_started" in consumed
    assert {"tool_called", "tool_returned", "tool_failed"} <= consumed

    missing = consumed - forwarded
    assert not missing, (
        "renderer consumes audit-events the transport does NOT forward — they "
        f"would vanish on the wire (A2 dual-stream bug): {sorted(missing)}"
    )
