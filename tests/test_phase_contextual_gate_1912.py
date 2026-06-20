"""Tier 2: the single shared contextual gate + the phase RouterLoop path (#1912a).

#1827 contextual narrowing was enforced only on the CHAT path. #1912 extracts the
check into one shared function (`tool_contextually_denied`) that every tool path
calls, and threads the per-session contextual to the **phase** RouterLoop
(Session→SkillRuntime→OSRuntime→PhaseExecutor→RouterLoop) so a narrowed agent's
skill act-turns are enforced too — closing the phase-path bypass.

These pin: (a) the shared gate; (b) a RouterLoop constructed the way the phase
path constructs it (with `contextual_permission`) blocks the denied tool at the
live gate, and an un-narrowed one does not (CLEAN RED contrast). The full
skill→phase→block integration + the control-IR op dispatch are #1912b.
"""
from __future__ import annotations

import asyncio
import json

from reyn.runtime.router_loop import RouterLoop
from reyn.security.permissions.effective import (
    ContextualPermission,
    tool_contextually_denied,
)


def test_shared_gate_denies_and_allows():
    """Tier 2: tool_contextually_denied — the single seam every path calls."""
    ctx = ContextualPermission(tool_deny=frozenset({"exec__sandboxed_exec"}))
    assert tool_contextually_denied(ctx, "exec__sandboxed_exec") is True
    assert tool_contextually_denied(ctx, "recall") is False
    # None contextual is inert (⊤) → byte-identical to pre-#1827.
    assert tool_contextually_denied(None, "exec__sandboxed_exec") is False


class _Host:
    agent_name = "t"

    def __init__(self):
        class _E:
            def emit(self, *a, **k): ...
        self.events = _E()
        self.calls: list = []

    async def sandboxed_exec(self, **kw):  # runs IFF executed
        self.calls.append(kw)
        return {"ok": True}


def _exec(loop: RouterLoop, name: str, args: dict) -> dict:
    return asyncio.run(
        loop._execute_tool({"function": {"name": name, "arguments": json.dumps(args)}})
    )


def test_phase_style_routerloop_blocks_denied_tool():
    """Tier 2: a RouterLoop built as the phase path builds it (with contextual)
    blocks the denied tool; an un-narrowed one does not (CLEAN RED contrast).

    The phase RouterLoop is the SAME class + gate as chat; #1912a's change is that
    PhaseExecutor now passes ``contextual_permission`` to it. Here we pin that such
    a RouterLoop blocks via the shared gate (native + invoke_action shapes).
    """
    ctx = ContextualPermission(tool_deny=frozenset({"exec__sandboxed_exec"}))
    narrowed = RouterLoop(host=_Host(), chain_id="p", max_iterations=5,
                          contextual_permission=ctx)
    assert _exec(narrowed, "exec__sandboxed_exec", {}).get("error", {}).get("kind") == "tool_excluded"
    assert _exec(narrowed, "invoke_action", {"action_name": "exec__sandboxed_exec"}).get("error", {}).get("kind") == "tool_excluded"

    # falsify: with no contextual (the pre-#1912 phase RouterLoop) it is NOT blocked
    open_loop = RouterLoop(host=_Host(), chain_id="p", max_iterations=5)
    assert _exec(open_loop, "exec__sandboxed_exec", {}).get("error", {}).get("kind") != "tool_excluded"
