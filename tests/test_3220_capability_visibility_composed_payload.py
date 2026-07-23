"""Tier 2: #3220 — capability_visibility_state's "tool" census matches the actual
per-turn COMPOSED ``tools=`` payload for the session's active chat-layer scheme,
not a raw global ``ToolDefinition`` registry census.

Ground truth (#3220 issue + architect-confirmed firm): the prior source,
``get_default_registry().names()``, enumerates every registered tool regardless of
whether the active scheme's composition path (``build_tools()`` / each
``ToolUseScheme.build_presentation``) ever advertises it — diverging from what the
LLM actually sees in three concrete ways this suite pins:

1. A ``gates.router="deny"`` phase-only tool (``ask_user``) is registry-visible but
   NEVER reachable in ANY scheme's composed payload — the OLD-bug case: it must not
   appear as authorized/visible.
2. ``universal-category`` folds individual/MCP capabilities behind the
   ``invoke_action`` wrapper — the fix EXPANDS the wrapper back to the underlying
   reachable capabilities (e.g. the ``mcp__*`` catalog actions), not the opaque
   wrapper name itself.
3. ``enumerate-all`` flattens ``base_tools() + catalog_entries()`` into the payload
   literally — both the legacy native names and the qualified catalog names must
   appear.

Real ``AgentRegistry`` + real ``Session`` (no mocks) — ``capability_visibility_state``
is exercised through the public ``Session.capability_visibility_state()`` API, same as
the sibling #2285 visibility-toggle suite.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _make_registry(tmp_path: Path, *, chat_tool_use_scheme: str) -> AgentRegistry:
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        s = make_session(
            agent_name=profile.name,
            state_log=state_log,
            registry=holder.get("reg"),
            chat_tool_use_scheme=chat_tool_use_scheme,
        )
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    return reg


async def _spawn(reg: AgentRegistry) -> Session:
    reg.get_or_load("alice")
    sid = await reg.spawn_session_recorded(
        "alice", presentation_consumer=None, intervention_bridge=None,
    )
    return reg.get_session("alice", sid)


@pytest.mark.asyncio
async def test_orphaned_phase_only_tool_absent_under_enumerate_all(tmp_path, monkeypatch):
    """Tier 2: OLD-bug-fixed proof (enumerate-all). ``ask_user`` (gates.router="deny",
    gates.phase="allow") is registered globally but never appears in ANY chat-layer
    scheme's composed ``tools=`` -- RED under the pre-#3220 registry-census source
    (which only filters by envelope, not payload-reachability); GREEN now."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path, chat_tool_use_scheme="enumerate-all")
    session = await _spawn(reg)

    state = session.capability_visibility_state()
    authorized_tools = {i["name"] for i in state["authorized"] if i["kind"] == "tool"}
    assert "ask_user" not in authorized_tools, (
        "a gates.router='deny' phase-only tool is never in any scheme's composed "
        "payload and must not be shown as visible"
    )
    # Sanity: the census is non-trivial (not accidentally emptied).
    assert "list_agents" in authorized_tools
    assert "delegate_to_agent" in authorized_tools


@pytest.mark.asyncio
async def test_orphaned_phase_only_tool_absent_under_universal_category(tmp_path, monkeypatch):
    """Tier 2: OLD-bug-fixed proof (universal-category) -- same orphan, different scheme."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path, chat_tool_use_scheme="universal-category")
    session = await _spawn(reg)

    state = session.capability_visibility_state()
    authorized_tools = {i["name"] for i in state["authorized"] if i["kind"] == "tool"}
    assert "ask_user" not in authorized_tools


@pytest.mark.asyncio
async def test_universal_category_expands_wrapper_to_reachable_capabilities(tmp_path, monkeypatch):
    """Tier 2: architect-confirmed granularity -- universal-category's composed
    ``tools=`` payload contains only the ``list_actions`` / ``describe_action`` /
    ``invoke_action`` wrapper meta-tools (individual capabilities are reachable
    THROUGH the wrapper, not named in the payload). The visibility census must
    EXPAND the wrapper back to those underlying reachable capabilities (e.g. the
    ``mcp__*`` catalog actions) and must NOT show the opaque wrapper name itself."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path, chat_tool_use_scheme="universal-category")
    session = await _spawn(reg)

    state = session.capability_visibility_state()
    authorized_tools = {i["name"] for i in state["authorized"] if i["kind"] == "tool"}

    # The wrapper plumbing names themselves are not "capabilities" -- not shown.
    assert "invoke_action" not in authorized_tools
    assert "list_actions" not in authorized_tools
    assert "describe_action" not in authorized_tools

    # The underlying catalog capability the wrapper makes reachable IS shown,
    # expanded to its real (qualified) name -- not the wrapper's name.
    assert "mcp__list_servers" in authorized_tools
    assert "mcp__call_tool" in authorized_tools

    # A router-only primitive that SURVIVES the universal wrapper-mode strip (still
    # literally advertised, not folded into the wrapper) stays visible too.
    assert "agent_spawn" in authorized_tools
    # A LEGACY per-kind name the wrapper mode DOES strip from tools= is reachable
    # only via the catalog now, not under its legacy native name.
    assert "delegate_to_agent" not in authorized_tools


@pytest.mark.asyncio
async def test_enumerate_all_shows_flattened_legacy_and_catalog_names(tmp_path, monkeypatch):
    """Tier 2: enumerate-all's composed payload literally unions
    ``base_tools() + catalog_entries()`` -- both the legacy native tool names AND
    the qualified catalog action names must be visible."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path, chat_tool_use_scheme="enumerate-all")
    session = await _spawn(reg)

    state = session.capability_visibility_state()
    authorized_tools = {i["name"] for i in state["authorized"] if i["kind"] == "tool"}

    # Legacy native name (literally advertised under enumerate-all).
    assert "delegate_to_agent" in authorized_tools
    # Qualified catalog action (also literally advertised, flattened alongside it).
    assert "mcp__list_servers" in authorized_tools
    assert "mcp__call_tool" in authorized_tools
    # enumerate-all never adds the universal wrapper meta-tools.
    assert "invoke_action" not in authorized_tools
