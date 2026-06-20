"""Tier 2: contextual capability narrowing via the conjunctive-∩ model (#1827 S1).

#1827 folds per-session contextual narrowing (delegation / topology / ephemeral)
into the existing `EffectivePermission` ∩-stack as one more restrict-only layer
(`ContextualLayer`) — NOT a new enforcement path. The `require_tool` gate gains an
optional `contextual` arg; `contextual=None` is byte-identical to the pre-#1827
gate.

never-elevate is the STRUCTURAL `all()` in `EffectivePermission.allows`: a
`ContextualLayer` is just another conjunct, so it can only narrow — it can neither
re-grant what it denies nor re-grant the static authority's deny.

Falsification gates (lead-required):
  - byte-identical: with `contextual=None` the gate decision (allow / the exact
    "not declared" deny message) is unchanged → breaking inertness goes CLEAN RED.
  - never-elevate: a `ContextualLayer` that "allows" a tool the static authority
    never granted must STILL be denied (no grant-back) → asserting the raise is
    the proof.

Policy: real `PermissionResolver` + real `EffectivePermission` + real gate; the
intervention bus (the only ask boundary) is a recording fake. No mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.security.permissions.effective import (
    AgentLayer,
    CapabilityAxis,
    ContextualLayer,
    ContextualPermission,
    EffectivePermission,
)
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import InterventionAnswer, UserIntervention


class _RecordingBus:
    """Real ask-boundary fake (mirrors test_permission_prompt_phrasing)."""

    def __init__(self, answer_id: str = "no") -> None:
        self.captured: list[UserIntervention] = []
        self._answer_id = answer_id

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.captured.append(iv)
        return InterventionAnswer(text=self._answer_id, choice_id=self._answer_id)


def _resolver(tmp_path: Path) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=True,
    )


# ── byte-identical: contextual=None is inert ────────────────────────────────


@pytest.mark.asyncio
async def test_none_contextual_allows_declared_tool(tmp_path):
    """Tier 2: contextual=None on a declared tool passes the layer (byte-identical).

    Falsify: if the contextual plumbing wrongly narrowed when None, a declared
    tool would be denied → CLEAN RED. 'yes' on the bus clears the _approve prompt.
    """
    r = _resolver(tmp_path)
    bus = _RecordingBus(answer_id="yes")
    decl = PermissionDecl(tool=["web_search"])
    # No raise = the layer admitted it (and the user approved).
    await r.require_tool(decl, "web_search", bus, contextual=None)


@pytest.mark.asyncio
async def test_none_contextual_preserves_undeclared_message(tmp_path):
    """Tier 2: contextual=None keeps the exact pre-#1827 'not declared' deny."""
    r = _resolver(tmp_path)
    bus = _RecordingBus(answer_id="yes")
    decl = PermissionDecl(tool=[])  # web_search NOT declared
    with pytest.raises(PermissionError, match="not declared in skill permissions"):
        await r.require_tool(decl, "web_search", bus, contextual=None)


# ── contextual narrowing (the new capability) ───────────────────────────────


@pytest.mark.asyncio
async def test_contextual_deny_blocks_declared_tool(tmp_path):
    """Tier 2: a declared tool denied by contextual tool_deny is blocked.

    The deny is decision-enabling (distinct message: blocked by context, not
    undeclared) and fires at the layer — before the _approve prompt.
    """
    r = _resolver(tmp_path)
    bus = _RecordingBus(answer_id="yes")
    decl = PermissionDecl(tool=["web_search"])
    ctx = ContextualPermission(tool_deny=frozenset({"web_search"}))
    with pytest.raises(PermissionError, match="blocked by the active capability context"):
        await r.require_tool(decl, "web_search", bus, contextual=ctx)
    assert bus.captured == [], "contextual deny must fire before the approve prompt"


@pytest.mark.asyncio
async def test_contextual_allowlist_narrows_to_subset(tmp_path):
    """Tier 2: a contextual tool_allow narrows a multi-tool decl to the subset."""
    r = _resolver(tmp_path)
    bus = _RecordingBus(answer_id="yes")
    decl = PermissionDecl(tool=["web_search", "file_read"])
    ctx = ContextualPermission(tool_allow=frozenset({"web_search"}))
    # web_search: declared ∩ contextual-allowed → passes.
    await r.require_tool(decl, "web_search", bus, contextual=ctx)
    # file_read: declared but NOT in the contextual allow-list → narrowed away.
    with pytest.raises(PermissionError, match="blocked by the active capability context"):
        await r.require_tool(decl, "file_read", bus, contextual=ctx)


# ── never-elevate (the structural invariant) ────────────────────────────────


@pytest.mark.asyncio
async def test_contextual_cannot_grant_back_static_deny(tmp_path):
    """Tier 2: a ContextualLayer that 'allows' an UNDECLARED tool cannot grant it.

    never-elevate falsification: the static authority never granted web_search
    (decl.tool empty); the context 'allows' it — but the gate must STILL deny
    (the static 'not declared' path wins). If grant-back were possible this would
    pass → asserting the raise is the proof.
    """
    r = _resolver(tmp_path)
    bus = _RecordingBus(answer_id="yes")
    decl = PermissionDecl(tool=[])  # NOT declared
    ctx = ContextualPermission(tool_allow=frozenset({"web_search"}))  # context "allows"
    with pytest.raises(PermissionError, match="not declared in skill permissions"):
        await r.require_tool(decl, "web_search", bus, contextual=ctx)


def test_effective_all_seam_is_never_elevate():
    """Tier 2: EffectivePermission.allows = all(layers) — the structural seam.

    Directly pins both never-elevate directions on the ∩ model itself:
      (a) static-grant ∩ contextual-deny → denied (contextual narrows);
      (b) static-deny ∩ contextual-allow → denied (no grant-back).
    """
    granted = PermissionDecl(tool=["web_search"])
    denied = PermissionDecl(tool=[])
    deny_ctx = ContextualLayer(ContextualPermission(tool_deny=frozenset({"web_search"})))
    allow_ctx = ContextualLayer(ContextualPermission(tool_allow=frozenset({"web_search"})))

    # (a) granted by AgentLayer, denied by ContextualLayer → all() = False.
    assert EffectivePermission([AgentLayer(granted), deny_ctx]).allows(
        CapabilityAxis.TOOL, "web_search"
    ) is False
    # (b) denied by AgentLayer, "allowed" by ContextualLayer → all() = False (no grant-back).
    assert EffectivePermission([AgentLayer(denied), allow_ctx]).allows(
        CapabilityAxis.TOOL, "web_search"
    ) is False
    # control: granted by both → True (the layer is genuinely inert when it permits).
    assert EffectivePermission([AgentLayer(granted), allow_ctx]).allows(
        CapabilityAxis.TOOL, "web_search"
    ) is True


def test_none_context_layer_is_top():
    """Tier 2: ContextualLayer(None) is ⊤ on every axis (inert)."""
    layer = ContextualLayer(None)
    assert layer.allows(CapabilityAxis.TOOL, "anything") is True
    assert layer.allows(CapabilityAxis.MCP, "anything") is True


# ── live gate: _excluded_result is now effective.py-backed (#1827 S1.5) ──────
#
# The LIVE tool-enforcement gate (router_loop._excluded_result, the #1406/#187
# pre-dispatch block) now consults the ∩-model (ContextualLayer) — the single
# enforcement gate. These pin that an explicit ContextualPermission blocks via
# every bypass shape (native / salvaged / direct invoke_action), and that the
# gate is load-bearing (no narrowing → the tool executes).
import asyncio
import json

from reyn.runtime.router_loop import RouterLoop


class _Events:
    def emit(self, *a, **k) -> None:
        pass


class _MiniHost:
    agent_name = "t"

    def __init__(self) -> None:
        self.events = _Events()
        self.web_search_calls: list[dict] = []

    async def web_search(self, **kw) -> dict:  # runs IFF the tool executes
        self.web_search_calls.append(kw)
        return {"kind": "web_search", "results": ["LEAKED GOLD"]}


def _exec(loop: RouterLoop, name: str, args: dict) -> dict:
    return asyncio.run(
        loop._execute_tool({"function": {"name": name, "arguments": json.dumps(args)}})
    )


def test_live_gate_blocks_via_explicit_contextual_all_paths():
    """Tier 2: an explicit ContextualPermission.tool_deny blocks the live gate via
    every bypass shape, and the excluded tool's handler never runs (#187)."""
    host = _MiniHost()
    loop = RouterLoop(
        host=host, chain_id="t", max_iterations=5,
        contextual_permission=ContextualPermission(tool_deny=frozenset({"web__search"})),
    )
    # (a) native, (b) salvaged (= native by name), (c) direct invoke_action.
    r_native = _exec(loop, "web__search", {"query": "gold?"})
    r_invoke = _exec(loop, "invoke_action", {"action_name": "web__search", "query": "gold?"})
    assert r_native.get("error", {}).get("kind") == "tool_excluded"
    assert r_invoke.get("error", {}).get("kind") == "tool_excluded"
    assert host.web_search_calls == [], "excluded handler must never run (no leak)"


def test_live_gate_is_load_bearing_gated_not_unconditional():
    """Tier 2: the live gate blocks ONLY when narrowing is present (falsify gate).

    With a tool_deny the dispatch returns the tool_excluded block; with no
    narrowing the same call is NOT blocked (it proceeds past the gate). Proves
    the block is gated, not unconditional. (Breaking the gate makes the all-paths
    block test above go CLEAN RED.)
    """
    deny_loop = RouterLoop(
        host=_MiniHost(), chain_id="t", max_iterations=5,
        contextual_permission=ContextualPermission(tool_deny=frozenset({"web__search"})),
    )
    assert _exec(deny_loop, "web__search", {}).get("error", {}).get("kind") == "tool_excluded"

    open_loop = RouterLoop(host=_MiniHost(), chain_id="t", max_iterations=5)
    assert _exec(open_loop, "web__search", {}).get("error", {}).get("kind") != "tool_excluded"


def test_live_gate_exclude_tools_bridge_preserves_block():
    """Tier 2: the legacy exclude_tools input bridges to the contextual gate.

    An existing caller passing exclude_tools (no explicit contextual) gets the
    SAME execution block through the new effective.py path — an unchanged result,
    so the #1406 / #187 callers keep their behaviour.
    """
    loop = RouterLoop(
        host=_MiniHost(), chain_id="t", max_iterations=5,
        exclude_tools={"web__search"},
    )
    blocked = _exec(loop, "invoke_action", {"action_name": "web__search"})
    assert blocked.get("error", {}).get("kind") == "tool_excluded"
    assert _exec(loop, "file__read", {}).get("error", {}).get("kind") != "tool_excluded"
