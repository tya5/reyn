"""Tier 2: contextual capability narrowing via the conjunctive-∩ model (#1827 S1).

#1827 folds per-session contextual narrowing (delegation / topology / ephemeral)
into the existing `EffectivePermission` ∩-stack as one more restrict-only layer
(`ContextualLayer`) — NOT a new enforcement path. The `require_tool` gate gains an
optional `contextual` arg; `contextual=None` is byte-identical to the pre-#1827
gate.

never-elevate is the STRUCTURAL `all()` in `EffectivePermission.allows`: a
`ContextualLayer` is just another conjunct, so it can only narrow — it can neither
re-grant what it denies nor re-grant the static authority's deny.

#5848 (architect ruling): `PermissionDecl.tool` (the static per-actor TOOL
declaration this file originally demonstrated never-elevate with) is deleted
— `AgentLayer` no longer has ANY opinion on the TOOL axis, so there is no
static grant/deny left to demonstrate the "no grant-back of a static deny"
half of never-elevate on TOOL specifically any more. The tests that
depended on an "undeclared tool" state being deniable are removed (that
state no longer exists); the tests that only need CONTEXTUAL narrowing
(deny / allow-subset) are kept, `decl=PermissionDecl()` throughout since
declaration is now irrelevant to this axis.
`test_effective_all_seam_is_never_elevate` demonstrates the FULL
never-elevate property (both directions) on the MCP axis instead — MCP
still has both a real `AgentLayer` grant/deny (`decl.mcp`) AND a
`ContextualLayer` narrowing (`mcp_allow`/`mcp_deny`), so it is the
faithful sibling this file's original TOOL-axis demonstration was.

Falsification gates (lead-required):
  - byte-identical: with `contextual=None` the gate decision (allow) is
    unchanged → breaking inertness goes CLEAN RED.
  - never-elevate: a `ContextualLayer` that "allows" a value the static
    authority never granted must STILL be denied (no grant-back) → the MCP
    version of this proof, below.

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
    NarrowingOrigin,
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
async def test_none_contextual_allows_a_tool(tmp_path):
    """Tier 2: contextual=None passes the layer (byte-identical) — #5848:
    AgentLayer no longer constrains TOOL at all, so any name passes here
    regardless of declaration; this test's own job is only the contextual
    plumbing itself staying inert at None.

    Falsify: if the contextual plumbing wrongly narrowed when None, this
    would be denied → CLEAN RED. 'yes' on the bus clears the _approve
    prompt.
    """
    r = _resolver(tmp_path)
    bus = _RecordingBus(answer_id="yes")
    decl = PermissionDecl()
    # No raise = the layer admitted it (and the user approved).
    await r.require_tool(decl, "web_search", bus, contextual=None)


# ── contextual narrowing (the new capability) ───────────────────────────────


@pytest.mark.asyncio
async def test_contextual_deny_blocks_a_tool(tmp_path):
    """Tier 2: a tool denied by contextual tool_deny is blocked — #5848:
    decl is irrelevant to this axis now, so this holds for EVERY decl, not
    just a "declared" one.

    The deny is decision-enabling (distinct message: blocked by context,
    not the — now unreachable — undeclared deny) and fires at the layer —
    before the _approve prompt. #3501: the "which narrowing" half is
    asserted by naming the term's own origin, so the message cannot
    degrade back to listing candidate narrowings.
    """
    r = _resolver(tmp_path)
    bus = _RecordingBus(answer_id="yes")
    decl = PermissionDecl()
    ctx = ContextualPermission(
        tool_deny=frozenset({"web_search"}),
        origin=NarrowingOrigin(
            label="the narrowing under test",
            cause="this test applied it",
            lifts_when="the test stops applying it",
        ),
    )
    with pytest.raises(PermissionError) as exc:
        await r.require_tool(decl, "web_search", bus, contextual=ctx)
    assert "the narrowing under test" in str(exc.value)
    assert bus.captured == [], "contextual deny must fire before the approve prompt"


@pytest.mark.asyncio
async def test_contextual_allowlist_narrows_to_subset(tmp_path):
    """Tier 2: a contextual tool_allow narrows access to the subset it
    names — #5848: decl is irrelevant to this axis now, so this holds
    regardless of what (if anything) a decl once would have declared."""
    r = _resolver(tmp_path)
    bus = _RecordingBus(answer_id="yes")
    decl = PermissionDecl()
    ctx = ContextualPermission(tool_allow=frozenset({"web_search"}))
    # web_search: in the contextual allow-list → passes.
    await r.require_tool(decl, "web_search", bus, contextual=ctx)
    # file_read: NOT in the contextual allow-list → narrowed away.
    with pytest.raises(PermissionError):
        await r.require_tool(decl, "file_read", bus, contextual=ctx)


# ── never-elevate (the structural invariant, demonstrated on MCP —
#    #5848's own note above: TOOL no longer has a static leg to elevate
#    over) ───────────────────────────────────────────────────────────────


def test_effective_all_seam_is_never_elevate():
    """Tier 2: EffectivePermission.allows = all(layers) — the structural seam.

    Directly pins both never-elevate directions on the ∩ model itself, on
    the MCP axis (still a real AgentLayer grant/deny + ContextualLayer
    narrowing pair — see this file's own module docstring for why TOOL no
    longer serves this demonstration post-#5848):
      (a) static-grant ∩ contextual-deny → denied (contextual narrows);
      (b) static-deny ∩ contextual-allow → denied (no grant-back).
    """
    granted = PermissionDecl(mcp=["filesystem"])
    denied = PermissionDecl(mcp=[])
    deny_ctx = ContextualLayer(ContextualPermission(mcp_deny=frozenset({"filesystem"})))
    allow_ctx = ContextualLayer(ContextualPermission(mcp_allow=frozenset({"filesystem"})))

    # (a) granted by AgentLayer, denied by ContextualLayer → all() = False.
    assert EffectivePermission([AgentLayer(granted), deny_ctx]).allows(
        CapabilityAxis.MCP, "filesystem"
    ) is False
    # (b) denied by AgentLayer, "allowed" by ContextualLayer → all() = False (no grant-back).
    assert EffectivePermission([AgentLayer(denied), allow_ctx]).allows(
        CapabilityAxis.MCP, "filesystem"
    ) is False
    # control: granted by both → True (the layer is genuinely inert when it permits).
    assert EffectivePermission([AgentLayer(granted), allow_ctx]).allows(
        CapabilityAxis.MCP, "filesystem"
    ) is True


def test_none_context_layer_is_top():
    """Tier 2: ContextualLayer(None) is ⊤ on every axis (inert)."""
    layer = ContextualLayer(None)
    assert layer.allows(CapabilityAxis.TOOL, "anything") is True
    assert layer.allows(CapabilityAxis.MCP, "anything") is True


# ── live gate: dispatch_tool's own 2b call-time restrict (#5841/#5854) ───────
#
# The LIVE tool-enforcement gate is `dispatch_tool`'s own 2b call-time
# restrict (`core/dispatch/dispatcher.py`) — the single enforcement gate.
# #5854 folded `RouterLoop`'s former, separate pre-dispatch `_excluded_result`
# gate into this one seam and retired it; `RouterLoop._execute_tool` now
# dispatches straight to `dispatch_tool`, which itself consults the ∩-model
# (ContextualLayer) before catalog membership. These pin that an explicit
# ContextualPermission blocks via every bypass shape (native / salvaged /
# direct invoke_action), and that the gate is load-bearing (no narrowing →
# the tool executes).
#
# #5848 note: these 4 tests are unrelated to `PermissionDecl.tool` (the
# deleted static declaration) — they exercise the live `dispatch_tool` gate
# against `ContextualPermission` directly, never touching a `PermissionDecl`
# at all. Kept verbatim.
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
        contextual_permission=ContextualPermission(tool_deny=frozenset({"web_search"})),
    )
    # (a) native, (b) salvaged (= native by name), (c) direct invoke_action.
    r_native = _exec(loop, "web_search", {"query": "gold?"})
    r_invoke = _exec(loop, "invoke_action", {"action_name": "web_search", "query": "gold?"})
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
        contextual_permission=ContextualPermission(tool_deny=frozenset({"web_search"})),
    )
    assert _exec(deny_loop, "web_search", {}).get("error", {}).get("kind") == "tool_excluded"

    open_loop = RouterLoop(host=_MiniHost(), chain_id="t", max_iterations=5)
    assert _exec(open_loop, "web_search", {}).get("error", {}).get("kind") != "tool_excluded"


def test_live_gate_exclude_tools_bridge_preserves_block():
    """Tier 2: the legacy exclude_tools input bridges to the contextual gate.

    An existing caller passing exclude_tools (no explicit contextual) gets the
    SAME execution block through the new effective.py path — an unchanged result,
    so the #1406 / #187 callers keep their behaviour.
    """
    loop = RouterLoop(
        host=_MiniHost(), chain_id="t", max_iterations=5,
        exclude_tools={"web_search"},
    )
    blocked = _exec(loop, "invoke_action", {"action_name": "web_search"})
    assert blocked.get("error", {}).get("kind") == "tool_excluded"
    assert _exec(loop, "read_file", {}).get("error", {}).get("kind") != "tool_excluded"
