"""Tier 3a: topology→profile→live-gate production-wiring e2e (#1827 S3).

The #1827 "actually works" proof. A capability_profile bound to a topology role
on disk → resolved by the real registry → its ContextualPermission blocks the
denied tool at the LIVE tool gate (router_loop._excluded_result, S1.5).

The denied tool is **`memory__write`, which is in NO `exclude_tools`** — so the
S1.5 exclude_tools→contextual bridge **cannot** block it. Only the explicit
topology→profile→gate thread can. So a green test proves the explicit production
wiring is LIVE; a silently-unwired thread → the tool executes → CLEAN RED.

Real registry + real on-disk YAML + real RouterLoop dispatch path. No mocks.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from reyn.runtime.registry import AgentRegistry
from reyn.runtime.router_loop import RouterLoop
from reyn.security.permissions.effective import ContextualPermission


def _write_project(tmp_path: Path) -> None:
    """A project where topology `t` binds member `worker` to profile `reviewer`
    whose tool_deny removes `memory__write` (a tool not in any exclude_tools)."""
    topo_dir = tmp_path / ".reyn" / "topologies"
    topo_dir.mkdir(parents=True)
    (topo_dir / "t.yaml").write_text(
        "name: t\nkind: network\nmembers: [worker, helper]\n"
        "profiles:\n  worker: reviewer\n",
        encoding="utf-8",
    )
    prof_dir = tmp_path / ".reyn" / "capability_profiles"
    prof_dir.mkdir(parents=True)
    (prof_dir / "reviewer.yaml").write_text(
        "name: reviewer\ntool_deny: [memory__write]\n", encoding="utf-8",
    )


def _registry(tmp_path: Path) -> AgentRegistry:
    return AgentRegistry(project_root=tmp_path, session_factory=lambda profile: None)


class _Host:
    agent_name = "worker"

    def __init__(self) -> None:
        class _E:
            def emit(self, *a, **k): ...
        self.events = _E()
        self.memory_write_calls: list = []

    async def memory_write(self, **kw):  # runs IFF the tool executes (the leak)
        self.memory_write_calls.append(kw)
        return {"kind": "memory_write", "ok": True}


def _exec(loop: RouterLoop, name: str, args: dict) -> dict:
    return asyncio.run(
        loop._execute_tool({"function": {"name": name, "arguments": json.dumps(args)}})
    )


def test_resolved_profile_blocks_denied_tool_at_live_gate(tmp_path: Path):
    """Tier 3a: disk profile → registry resolve → ContextualPermission blocks
    memory__write at the live gate; an unrelated tool is unaffected."""
    _write_project(tmp_path)
    reg = _registry(tmp_path)

    contextual, excluded = reg.resolved_profile_for("worker")
    # the resolution loaded the profile from disk
    assert isinstance(contextual, ContextualPermission)
    assert "memory__write" in contextual.tool_deny
    assert excluded == frozenset()  # no categories in the profile → no view narrowing

    loop = RouterLoop(host=_Host(), chain_id="c", max_iterations=5,
                      contextual_permission=contextual)
    # memory__write is denied by the profile (and is in NO exclude_tools).
    blocked = _exec(loop, "memory__write", {"k": "v"})
    assert blocked.get("error", {}).get("kind") == "tool_excluded"
    # the via-invoke_action bypass shape is also blocked.
    blocked2 = _exec(loop, "invoke_action", {"action_name": "memory__write"})
    assert blocked2.get("error", {}).get("kind") == "tool_excluded"


def test_unbound_agent_resolves_to_none_inert(tmp_path: Path):
    """Tier 3a: an agent with no profile binding → (None, ∅) → live gate inert.

    `helper` is a member of topology `t` but has no profile → no narrowing, so
    its session is byte-identical to pre-#1827 (the gate does not block)."""
    _write_project(tmp_path)
    reg = _registry(tmp_path)

    contextual, excluded = reg.resolved_profile_for("helper")
    assert contextual is None and excluded == frozenset()

    # a loop with no contextual does not block memory__write at the gate
    loop = RouterLoop(host=_Host(), chain_id="c", max_iterations=5,
                      contextual_permission=contextual)
    assert _exec(loop, "memory__write", {}).get("error", {}).get("kind") != "tool_excluded"


def test_falsify_memory_write_absent_from_exclude_tools(tmp_path: Path):
    """Tier 3a: the bridge cannot achieve this block (falsification anchor).

    A loop given memory__write via exclude_tools would block it — but the
    production path binds NO exclude_tools; the block above comes solely from the
    resolved profile's ContextualPermission. This pins that the denied tool is
    not a default/bridge value, so the e2e proves the EXPLICIT thread is live.
    """
    _write_project(tmp_path)
    reg = _registry(tmp_path)
    contextual, _ = reg.resolved_profile_for("worker")
    # No exclude_tools anywhere in the production path for memory__write.
    loop = RouterLoop(host=_Host(), chain_id="c", max_iterations=5,
                      contextual_permission=contextual)
    # The block is contextual-sourced: a sibling loop with neither contextual nor
    # exclude_tools does NOT block (so the block is not incidental).
    open_loop = RouterLoop(host=_Host(), chain_id="c", max_iterations=5)
    assert _exec(open_loop, "memory__write", {}).get("error", {}).get("kind") != "tool_excluded"
    assert _exec(loop, "memory__write", {}).get("error", {}).get("kind") == "tool_excluded"
