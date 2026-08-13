"""Tier 2: the A2A + MCP progress fan-outs forward only LIVE audit-event kinds,
from one shared declaration (#3357).

Both remote progress bridges subscribe to the same source — the session's chat
audit-event log (``Session._audit_events``) — and forward a selection of kinds as
progress notifications. Two invariants, asserted separately:

- **The mechanism is honest.** Every kind in ``PROGRESS_LIFECYCLE_EVENTS`` has a
  live emit call-site in ``src/reyn``. The audit-event ``type`` namespace is
  OPEN (no closed vocabulary, no emit-time kind check), so a kind nobody emits
  degrades the stream silently — the ordinal counter keeps incrementing and a
  subscribing peer cannot tell a degraded stream from a quiet run. That is
  exactly how ``phase_started`` / ``act_executed`` survived the phase engine's
  deletion inside two live network protocols.
- **Production reaches it.** Both bridges hold that constant (not a private
  copy), render through the shared formatter, and the MCP ``initialize``
  advertisement derives its ``events`` list from the same constant instead of
  restating it.

Real instances throughout: a real ``EventLog``, the real bridges, the real
``build_init_options`` output built from a real ``Server``.
"""
from __future__ import annotations

import ast
import asyncio

import pytest

from reyn.core.events.events import EventLog
from reyn.core.events.progress_lifecycle import (
    PROGRESS_LIFECYCLE_EVENTS,
    format_progress_message,
)
from tests._support.paths import REPO_ROOT

_SRC = REPO_ROOT / "src" / "reyn"
# The declaration module names every kind in prose. An AST walk already ignores
# prose, so this exclusion is belt-and-braces: it also rules out a future
# illustrative ``emit(...)`` inside the declaration vouching for its own member.
_DECLARATION = _SRC / "core" / "events" / "progress_lifecycle.py"


def _emitted_kind(node: ast.AST) -> str | None:
    """The kind string of an ``<x>.emit("<kind>", …)`` / ``emit("<kind>", …)`` CALL.

    ``None`` for anything that is not such a call. Only an ``ast.Call`` whose
    callee is named ``emit`` and whose first positional argument is a string
    constant counts — which is what makes this an *emitter* census rather than a
    text census (see the gate's docstring).
    """
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func = node.func
    name = (
        func.attr if isinstance(func, ast.Attribute)
        else func.id if isinstance(func, ast.Name)
        else None
    )
    if name != "emit":
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _kinds_emitted_in_src() -> set[str]:
    """Every string literal passed as the first positional arg of an ``emit`` call."""
    emitted: set[str] = set()
    for py in sorted(_SRC.rglob("*.py")):
        if "__pycache__" in py.parts or py == _DECLARATION:
            continue
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            kind = _emitted_kind(node)
            if kind is not None:
                emitted.add(kind)
    return emitted


# ── 1. The mechanism: every forwarded kind has a live producer ─────────────


def test_every_forwarded_kind_has_a_live_emit_call_site() -> None:
    """Tier 2: each kind in ``PROGRESS_LIFECYCLE_EVENTS`` is emitted by a real
    producer in ``src/reyn``.

    ★ The census is an **AST walk**, not a text scan, and the distinction is
    load-bearing rather than stylistic. A ``re.search`` for ``emit("<kind>"``
    matches that literal wherever it appears — including inside a docstring or a
    comment. This module, and the modules it guards, discuss these kinds in
    prose extensively; under a text scan, someone adding a fifth kind who writes
    the docstring before the emitter would get a green gate over a dead kind,
    i.e. #3357 recurring through its own guard. Counting only ``ast.Call`` nodes
    whose callee is ``emit`` and whose first argument is a string constant means
    *only an actual call* can vouch for a member.

    Add a kind no producer emits → RED (that is the #3357 defect reproduced).
    """
    emitted = _kinds_emitted_in_src()
    dead = PROGRESS_LIFECYCLE_EVENTS - emitted
    assert not dead, (
        "progress fan-out declares audit-event kinds with no emit call-site in "
        f"src/reyn (nothing produces them; peers see a silently degraded "
        f"stream): {sorted(dead)}"
    )


def test_formatter_labels_every_forwarded_kind_distinctly() -> None:
    """Tier 2: the shared formatter renders a dedicated line per forwarded kind
    — never the fall-through that just echoes the kind name. A kind added to the
    constant without a message arm would reach peers as a bare type string."""
    labels = {
        kind: format_progress_message(kind, {})
        for kind in PROGRESS_LIFECYCLE_EVENTS
    }
    for kind, label in labels.items():
        assert label != kind, f"{kind!r} has no message arm in the formatter"
    rendered = sorted(labels.values())
    assert rendered == sorted(set(labels.values())), (
        f"two forwarded kinds render to the same progress line: {labels}"
    )


def test_formatter_reads_the_identifying_field_of_each_kind() -> None:
    """Tier 2: each arm reads the field the real emitter carries — ``kind`` on
    ``turn_started``, ``model`` on ``llm_called``, ``tool`` on the tool-dispatch
    pair — so the progress line names what happened.

    ★ What this arm does NOT cover, written here so the next reader sees it
    without re-deriving it. Two gaps, both from the expectations being
    hand-written rather than enumerated from ``PROGRESS_LIFECYCLE_EVENTS``:

      - **A fifth member sits outside this arm silently.** Enumerating is not
        available: the assertion IS the expected string, so there is nothing to
        generate it from. The two arms above are the total ones (every member
        has a live emitter; every member has a distinct message arm) — this one
        adds "and reads the right field", for the kinds it names.
      - **``tool_failed`` is asserted more weakly than the other three.** All
        four kinds ARE exercised below, but its check is substring + inequality
        rather than an exact string, so a reworded failure suffix stays green
        here. Deliberate: the exact suffix is presentation, and the property
        that matters — the failure leg is distinguishable from the success leg —
        is what is asserted."""
    assert format_progress_message("turn_started", {"kind": "user"}) == "turn: user"
    assert format_progress_message("llm_called", {"model": "sonnet"}) == "llm: sonnet"
    assert format_progress_message("tool_returned", {"tool": "grep"}) == "tool: grep"
    failed = format_progress_message("tool_failed", {"tool": "grep"})
    assert "grep" in failed and failed != "tool: grep"


# ── 2. Production reaches it: one declaration, both bridges ────────────────


def test_both_bridges_forward_the_shared_declaration() -> None:
    """Tier 2: the A2A and MCP bridges filter on the shared constant itself, so
    the two protocols cannot drift apart. Give either bridge a private copy →
    this still passes on equality but RED on identity, which is the point: the
    copy is what allowed #3357's two dead kinds to sit in both files."""
    pytest.importorskip("mcp", reason="MCP SDK not installed")
    from reyn.interfaces.web.routers.a2a import _A2AProgressBridge
    from reyn.mcp.server import _MCPProgressBridge

    assert _A2AProgressBridge.TRACKED_EVENTS is PROGRESS_LIFECYCLE_EVENTS
    assert _MCPProgressBridge.TRACKED_EVENTS is PROGRESS_LIFECYCLE_EVENTS


def test_mcp_initialize_advertises_the_forwarded_kinds() -> None:
    """Tier 2: the ``reyn.progress.skill_lifecycle`` experimental capability the
    MCP ``initialize`` response carries lists exactly the kinds the bridge
    forwards — derived from the constant, not restated. Hardcode the list back →
    RED as soon as the forwarded set changes (the claim/reality gate #271 M3
    asked for, previously a source-text pin that instead FROZE the dead kinds)."""
    pytest.importorskip("mcp", reason="MCP SDK not installed")
    from mcp.server import Server

    from reyn.mcp.server import build_init_options

    init_options = build_init_options(Server("reyn"))

    experimental = init_options.capabilities.experimental
    assert experimental is not None
    progress = experimental["reyn.progress.skill_lifecycle"]
    assert progress["events"] == sorted(PROGRESS_LIFECYCLE_EVENTS)


# ── 3. End-to-end through a real EventLog ──────────────────────────────────


def test_a2a_bridge_forwards_a_live_tool_dispatch_audit_event() -> None:
    """Tier 2: emitting the audit-event kinds a real turn produces
    (``turn_started`` then ``tool_returned``) on a real ``EventLog`` reaches the
    A2A bridge's SSE sink with legible messages and a monotonic ordinal.

    Pins the whole subscribe → filter → format → dispatch path against the kinds
    production actually emits, which is what the pre-#3357 suite could not do:
    it only ever emitted kinds no producer wrote."""
    from reyn.interfaces.web.routers.a2a import _A2AProgressBridge
    from reyn.interfaces.web.run_registry import RunRegistry

    events = EventLog()

    # ``Session`` is not cheaply constructible; the bridge reads exactly one
    # attribute off it (the chat audit-event log), and that log is the real
    # ``EventLog`` production dispatches through.
    class _SessionWithChatEvents:
        _audit_events = events

    run_registry = RunRegistry()
    entry = run_registry.create(agent_name="demo", chain_id="c1")
    bridge = _A2AProgressBridge(
        session=_SessionWithChatEvents(),
        run_id=entry.run_id,
        webhook_url=None,
        agent_name="demo",
        run_registry=run_registry,
    )

    async def _drive() -> None:
        bridge.attach()
        try:
            events.emit("turn_started", kind="user", chain_id="c1")
            events.emit("tool_returned", tool="grep", chain_id="c1")
            events.emit("llm_response_received", cost_usd=0.1)  # not forwarded
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        finally:
            bridge.detach()

    asyncio.run(_drive())

    forwarded = run_registry.get(entry.run_id).history_events
    assert [p["event"] for p in forwarded] == ["turn_started", "tool_returned"]
    assert [p["message"] for p in forwarded] == ["turn: user", "tool: grep"]
    assert [p["progress"] for p in forwarded] == [1, 2]
