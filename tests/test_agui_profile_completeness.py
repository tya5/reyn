"""Tier 2: every Custom-mapped reyn frame has an extension-profile entry (P4, SR4).

The reyn ``reyn.*`` Custom namespace is a documented, tested extension profile
(:mod:`reyn.interfaces.transport.agui.profile`). This gate keeps it honest and
non-circular: it enumerates the Custom-mapped frame vocabulary *from the
renderer's source vocabulary* — the display kinds the renderer dispatches on
(AST-scanned) plus the derived ``renderer_chat_events`` — encodes each through
the codec, collects the ``reyn.*`` ``name`` of every event that lands on
``CUSTOM``, and asserts each such name has a profile entry. It reads the codec's
output, never the profile itself, so it is not comparing the profile to itself.

An emitted Custom name with no profile entry is RED — doc-drift is designed out,
the same discipline as the P1/P2 completeness gates.

Real instances only — the real codec + the real profile registry; no mocks.
"""
from __future__ import annotations

import ast
from pathlib import Path

from reyn.core.events.events import Event
from reyn.interfaces.transport.agui.profile import is_profiled, profiled_names
from reyn.interfaces.transport.agui.protocol import (
    CUSTOM,
    REASONING_MESSAGE_CONTENT,
    encode_frame,
    encode_intervention_tool_start,
)
from reyn.interfaces.transport.frames import (
    DisplayFrame,
    EventFrame,
    renderer_chat_events,
)
from reyn.runtime.outbox import OutboxMessage

_RENDERER = (
    Path(__file__).resolve().parents[1]
    / "src" / "reyn" / "interfaces" / "repl" / "renderer.py"
)
_DISPLAY_DISPATCH_FUNCS = {"message", "format_inline_message"}


def _renderer_display_kinds() -> set[str]:
    """Every ``kind`` literal the renderer's display-dispatch functions compare
    against — the DisplayFrame vocabulary, read from renderer source (not the
    codec's tables), so the enumeration is independent of the profile."""
    tree = ast.parse(_RENDERER.read_text(encoding="utf-8"))
    kinds: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name in _DISPLAY_DISPATCH_FUNCS):
            continue
        for cmp_node in ast.walk(node):
            if not isinstance(cmp_node, ast.Compare):
                continue
            for lit in ast.walk(cmp_node):
                if isinstance(lit, ast.Constant) and isinstance(lit.value, str):
                    kinds.add(lit.value)
    return kinds


def _emitted_custom_names() -> set[str]:
    """The ``reyn.*`` Custom names the codec puts on the wire for the source
    vocabulary — derived from the renderer's kinds/events, not from the profile."""
    names: set[str] = set()
    for kind in _renderer_display_kinds():
        ev = encode_frame(DisplayFrame(OutboxMessage(kind=kind, text="x")))
        if ev.type == CUSTOM:
            names.add(ev.data["name"])
    for etype in renderer_chat_events():
        ev = encode_frame(EventFrame(Event(type=etype, data={})))
        if ev.type == CUSTOM:
            names.add(ev.data["name"])
    return names


def test_every_custom_mapped_frame_is_profiled() -> None:
    """Tier 2: each reyn.* Custom name the codec emits has an extension-profile
    entry. An unprofiled Custom name ⇒ RED (doc-drift, designed out)."""
    emitted = _emitted_custom_names()

    # Sanity: the enumeration found the real Custom vocabulary (a broken scan
    # that found nothing must not vacuously pass).
    assert {"reyn.display.trace", "reyn.event.user_answered_intervention"} <= emitted

    missing = {name for name in emitted if not is_profiled(name)}
    assert not missing, f"unprofiled reyn.* Custom names (add to profile): {sorted(missing)}"


def test_intervention_frontend_tool_toolname_is_profiled() -> None:
    """Tier 2: the HITL frontend-tool ``toolName`` the emitter really produces
    falls under a profiled reyn.intervention.* namespace — for every intervention
    kind (open namespace). Unprofiled ⇒ RED (the P3 members were the stale-base gap)."""
    # Real emitter output for representative intervention kinds (ask_user free-text
    # + a permission.* prompt) — enumerated from the codec, not the profile.
    for kind in ("ask_user", "permission.grant_deny"):
        ev = encode_intervention_tool_start(
            {"intervention_id": "iv-1", "intervention_kind": kind, "prompt": "?"}
        )
        toolname = ev.data["toolName"]
        assert toolname.startswith("reyn.intervention."), toolname
        assert is_profiled(toolname), f"unprofiled intervention frontend-tool: {toolname}"

    # The empty-kind fallback (``reyn.intervention.ask_user``) is also profiled.
    ev = encode_intervention_tool_start({"intervention_id": "iv-2", "intervention_kind": ""})
    assert is_profiled(ev.data["toolName"])


def test_reasoning_is_standard_mapped_not_a_custom_profile_entry() -> None:
    """Tier 2: P6a — the reasoning frame is STANDARD-mapped (a Reasoning* event),
    so it needs NO reyn.* Custom profile entry. The completeness gate must expect
    reasoning as standard-mapped, not demand a ``reyn.display.reasoning`` Custom
    entry for a now-standard signal.

    Strip-falsify: remove ``"reasoning"`` from ``_DISPLAY_KIND_EVENT`` → the frame
    reverts to CUSTOM (``reyn.display.reasoning``) → the standard-mapped assertion
    below goes RED (and the general completeness gate would then demand a Custom
    entry that no longer exists)."""
    ev = encode_frame(DisplayFrame(OutboxMessage(kind="reasoning", text="thinking")))

    # Standard-mapped, not CUSTOM: no reyn.* Custom name is emitted for reasoning.
    assert ev.type == REASONING_MESSAGE_CONTENT
    assert ev.type != CUSTOM
    assert "name" not in ev.data, "a standard Reasoning* event carries no CUSTOM name"

    # The move's consequence: there is NO Custom profile entry for reasoning, and
    # the completeness gate does not require one (a standard event needs no
    # extension-profile entry).
    assert "reyn.display.reasoning" not in profiled_names()
    assert "reyn.display.reasoning" not in _emitted_custom_names()
