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
    the qualified catalog action names must be visible.

    #3224 (merged on top of this branch): ``EnumerateAllScheme.build_presentation``
    EXCLUDES ``mcp__call_tool`` from its own flattened catalog union (already
    covered by the native ``call_mcp_tool``, so the model no longer sees the same
    MCP-call action twice) -- a transform inside the scheme method itself, not
    the raw ``catalog_entries()`` building block. The census must reflect that
    exclusion too (i.e. NOT show ``mcp__call_tool``), because it now sources
    from the scheme's real ``build_presentation``, not a parallel re-derivation
    of "base_tools + catalog_entries" that has no way to see a scheme-owned
    transform layered on top."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path, chat_tool_use_scheme="enumerate-all")
    session = await _spawn(reg)

    state = session.capability_visibility_state()
    authorized_tools = {i["name"] for i in state["authorized"] if i["kind"] == "tool"}

    # Legacy native name (literally advertised under enumerate-all).
    assert "delegate_to_agent" in authorized_tools
    # Qualified catalog action (also literally advertised, flattened alongside it).
    assert "mcp__list_servers" in authorized_tools
    # #3224: enumerate-all's OWN build_presentation excludes this one qualified
    # catalog action (duplicate of the native call_mcp_tool) -- absent here too.
    assert "mcp__call_tool" not in authorized_tools
    # enumerate-all never adds the universal wrapper meta-tools.
    assert "invoke_action" not in authorized_tools


class _OracleOps:
    """Tier 2 test-local Fake ``SchemeOps`` (protocol-conforming, hand-written —
    NOT an import of ``capability_visibility``'s own ``_VisibilityProbeOps``) that
    supplies the same 3 host-derived raw ingredients (``present`` / ``base_tools`` /
    ``catalog_entries``) from a REAL ``RouterHostAdapter`` (``session.router_host``,
    the public test-probe accessor), so calling a scheme's real ``build_presentation``
    through this independently-written ops gives an INDEPENDENT oracle for "what does
    this scheme actually compose for this session" — not a re-check of production
    code against itself."""

    def __init__(self, router_host) -> None:
        self._host = router_host

    def present(self, available, layer_ctx):
        from reyn.runtime.router_tools import build_tools
        from reyn.tools.scheme import Presentation

        return Presentation(llm_tools_payload=build_tools(
            self._host.list_available_agents(),
            file_permissions=self._host.get_file_permissions(),
            mcp_servers=self._host.get_mcp_servers(),
            web_fetch_allowed=self._host.get_web_fetch_allowed(),
            universal_wrappers_enabled=layer_ctx["univ_enabled"],
            search_actions_visible=layer_ctx.get("search_visible", False),
        ))

    def base_tools(self, available, layer_ctx):
        from reyn.runtime.router_tools import build_tools

        return build_tools(
            self._host.list_available_agents(),
            file_permissions=self._host.get_file_permissions(),
            mcp_servers=self._host.get_mcp_servers(),
            web_fetch_allowed=self._host.get_web_fetch_allowed(),
            universal_wrappers_enabled=False,
        )

    async def catalog_entries(self):
        from reyn.tools import universal_catalog
        from reyn.tools.types import RouterCallerState, ToolContext

        ctx = ToolContext(
            events=None, permission_resolver=None, workspace=None, caller_kind="router",
            router_state=RouterCallerState(sandbox_backend=self._host.get_sandbox_backend()),
        )
        return [
            {"type": "function", "function": e}
            for e in universal_catalog.catalog_entries(ctx)
        ]


async def _oracle_payload_names(session: Session, scheme_name: str) -> "tuple[set[str], set[str]]":
    """Independently compute (payload_names, catalog_names) for ``scheme_name`` by
    calling the REAL registered ``ToolUseScheme.build_presentation`` through
    ``_OracleOps`` bound to the session's real ``router_host``. ``payload_names`` =
    what the scheme actually puts in ``tools=`` (or ``dispatchable_catalog`` for
    CodeAct); ``catalog_names`` = the full ``universal_catalog`` action set (the
    wrapper-expansion target for universal-category)."""
    from reyn.tools.scheme import flat_catalog_entries, get_scheme

    ops = _OracleOps(session.router_host)
    scheme = get_scheme(scheme_name)
    available = {"hot_list_aliases": [], "exclude_tools": frozenset()}
    layer_ctx = {
        "univ_enabled": scheme_name == "universal-category",
        "search_visible": False,
        "ctx_signal_present": False,
        "router_model": None,
        "router_model_family": "other",
        "non_interactive": True,
        "available_skills": None,
    }
    pres = await scheme.build_presentation(available, layer_ctx, ops=ops)
    payload_names = {e["name"] for e in flat_catalog_entries(pres.llm_tools_payload)}
    if pres.dispatchable_catalog is not None:
        payload_names = {e["name"] for e in flat_catalog_entries(pres.dispatchable_catalog)}
    catalog_names = {e["name"] for e in flat_catalog_entries(await ops.catalog_entries())}
    return payload_names, catalog_names


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme_name", ["enumerate-all", "codeact"])
async def test_visibility_census_exactly_matches_composed_payload(tmp_path, monkeypatch, scheme_name):
    """Tier 2: architect-required conformance test. capability_visibility_state's
    "tool" census must EXACTLY equal the reachable-capability set of the scheme's
    OWN real ``build_presentation()`` output — independently recomputed here via
    ``_OracleOps`` (a hand-written Fake, not importing ``capability_visibility``'s
    production ``_VisibilityProbeOps``) driving the SAME real registered
    ``ToolUseScheme`` class.

    RED under a parallel ``build_tools()`` + ``universal_catalog.catalog_entries()``
    re-derivation (this PR's first cut, and the co-vet finding that triggered this
    test): under merged #3224, the enumerate-all payload excludes ``mcp__call_tool``
    (a transform ``EnumerateAllScheme.build_presentation`` applies on top of the raw
    ``catalog_entries()`` ingredient) — a parallel re-derivation that calls
    ``catalog_entries()`` directly has no way to see that exclusion and keeps
    ``mcp__call_tool`` in its set, so it would NOT equal this oracle's set (which
    reflects the real scheme transform). GREEN once the source is
    ``build_presentation()`` itself (this PR's final cut)."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path, chat_tool_use_scheme=scheme_name)
    session = await _spawn(reg)

    state = session.capability_visibility_state()
    authorized_tools = {i["name"] for i in state["authorized"] if i["kind"] == "tool"}

    payload_names, _ = await _oracle_payload_names(session, scheme_name)
    assert authorized_tools == payload_names, (
        f"scheme={scheme_name!r}: visibility census must exactly equal the scheme's "
        f"real composed payload. only-in-authorized={authorized_tools - payload_names} "
        f"only-in-payload={payload_names - authorized_tools}"
    )


@pytest.mark.asyncio
async def test_universal_category_census_conforms_with_wrapper_expansion(tmp_path, monkeypatch):
    """Tier 2: architect-required conformance test, universal-category variant.
    the wrapper meta-tool names in the scheme's real composed payload must NOT
    appear authorized (they are plumbing, not capabilities); every OTHER name the
    real payload advertises (a legacy primitive that survives the wrapper-mode
    strip) MUST appear authorized; and the full catalog action set (what the
    payload's ``invoke_action`` makes reachable) MUST appear authorized too — the
    wrapper-expansion behavior, verified against the real ``build_presentation()``
    output rather than a re-derivation."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path, chat_tool_use_scheme="universal-category")
    session = await _spawn(reg)

    state = session.capability_visibility_state()
    authorized_tools = {i["name"] for i in state["authorized"] if i["kind"] == "tool"}

    payload_names, catalog_names = await _oracle_payload_names(session, "universal-category")
    wrapper_names = {"list_actions", "describe_action", "invoke_action", "search_actions"}
    survivor_names = payload_names - wrapper_names

    assert not (wrapper_names & authorized_tools), "wrapper plumbing names must not be shown as capabilities"
    assert survivor_names <= authorized_tools, "a legacy primitive still literally in the real payload must stay visible"
    assert catalog_names <= authorized_tools, "the catalog the wrapper makes reachable must be expanded into view"
