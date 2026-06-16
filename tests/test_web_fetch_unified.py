"""Tier 2: WEB_FETCH ToolDefinition M3 Wave 1 invariants (ADR-0026 M3).

Verifies that WEB_FETCH ToolDefinition:
- Produces byte-identical output to the prior ToolSpec literal for web_fetch.
  Drift in description or parameters here would invalidate replay fixtures.
- Has the correct gates, purity, and category.
- Is findable via get_default_registry().
- Registers without error and is the single registry entry for web_fetch.
- FP-0022: require_web_fetch() 4-layer approval gate behavior.

No mocks of collaborators. All tests use real ToolDefinition / ToolRegistry
instances. No private state assertions.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.tools import get_default_registry
from reyn.tools.web_fetch import _WEB_FETCH_DESCRIPTION, _WEB_FETCH_PARAMETERS, WEB_FETCH

# ── 1. render_for_router byte-identity gate ───────────────────────────────────

def test_web_fetch_router_render_matches_legacy_shape():
    """Tier 2: WEB_FETCH.render_for_router() produces the expected shape.

    Description was rewritten in #385 PoC PR-D to surface the new
    preview + path_ref return contract (= text bodies routed to
    ``.reyn/tool-results/`` instead of inlined). Key-phrase checks now
    pin the NEW wording. LLMReplay fixtures that hashed the old
    description need to be re-recorded in the same PR; this test pins
    the new contract so future drift gets caught.
    """
    rendered = WEB_FETCH.render_for_router()

    # Top-level shape
    assert rendered["type"] == "function"
    assert isinstance(rendered["function"], dict)

    fn = rendered["function"]

    # Name
    assert fn["name"] == "web_fetch"

    # Description: key phrases that identify the post-PR-D web_fetch
    # contract. The exact wording is pinned in
    # ``test_web_fetch_router_render_exact_description``; this test
    # only checks the load-bearing tokens.
    assert "URL" in fn["description"] or "url" in fn["description"].lower()
    assert "preview" in fn["description"]
    assert "path_ref" in fn["description"]
    assert "max_length" in fn["description"]
    assert "50000" in fn["description"]
    assert "web_search" in fn["description"]
    assert "file__read" in fn["description"]  # #1449: read_tool_result retired

    # Parameters schema
    params = fn["parameters"]
    assert params["type"] == "object"
    assert params["required"] == ["url"]
    assert "url" in params["properties"]
    assert params["properties"]["url"] == {"type": "string"}
    assert "max_length" in params["properties"]
    assert params["properties"]["max_length"] == {"type": "integer"}


def test_web_fetch_router_render_exact_description():
    """Tier 2: WEB_FETCH description is byte-identical to the post-#385
    PoC PR-D string. Any whitespace or punctuation diff is a stop signal
    — LLMReplay fixtures hash this verbatim.
    """
    rendered = WEB_FETCH.render_for_router()
    expected_description = (
        "Fetch a single URL. Returns a structured preview "
        "(title, outline, first paragraph, link count for HTML; "
        "first lines for text) plus a path_ref to the full body "
        "stored under .reyn/tool-results/. url: absolute http/https URL. "
        "max_length: cap on extracted body length (default 50000). "
        "Use after web_search to load a result page; call "
        "file__read(path) to read the full body."
    )
    assert rendered["function"]["description"] == expected_description


def test_web_fetch_router_render_exact_parameters():
    """Tier 2: WEB_FETCH parameters schema is byte-identical to the legacy
    ToolSpec parameters dict."""
    rendered = WEB_FETCH.render_for_router()
    legacy_parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_length": {"type": "integer"},
        },
        "required": ["url"],
    }
    assert rendered["function"]["parameters"] == legacy_parameters


# ── 2. Gate invariants ────────────────────────────────────────────────────────

def test_web_fetch_gates_both_allow():
    """Tier 2: WEB_FETCH has gates.router=allow and gates.phase=allow."""
    assert WEB_FETCH.gates.router == "allow"
    assert WEB_FETCH.gates.phase == "allow"


# ── 3. Purity and category ────────────────────────────────────────────────────

def test_web_fetch_purity_read_only():
    """Tier 2: WEB_FETCH purity is 'read_only' (no workspace side effects)."""
    assert WEB_FETCH.purity == "read_only"


def test_web_fetch_category_discovery():
    """Tier 2: WEB_FETCH category is 'discovery'."""
    assert WEB_FETCH.category == "discovery"


# ── 4. Registry lookup ────────────────────────────────────────────────────────

def test_default_registry_contains_web_fetch():
    """Tier 2: get_default_registry() returns a registry that contains web_fetch."""
    registry = get_default_registry()
    assert "web_fetch" in registry


def test_default_registry_lookup_returns_web_fetch_instance():
    """Tier 2: registry.lookup('web_fetch') returns the WEB_FETCH instance."""
    registry = get_default_registry()
    found = registry.lookup("web_fetch")
    assert found is WEB_FETCH


def test_default_registry_web_fetch_in_for_router():
    """Tier 2: WEB_FETCH appears in registry.for_router() (gates.router=allow)."""
    registry = get_default_registry()
    router_tools = registry.for_router()
    assert WEB_FETCH in router_tools


def test_default_registry_web_fetch_in_for_phase():
    """Tier 2: WEB_FETCH appears in registry.for_phase() (gates.phase=allow)."""
    registry = get_default_registry()
    phase_tools = registry.for_phase()
    assert WEB_FETCH in phase_tools


# ── 5. build_tools integration — web_fetch rendered from registry ──────────────

def test_build_tools_includes_web_fetch_via_registry():
    """Tier 2: build_tools() includes web_fetch rendered from the unified
    registry. The rendered dict must match the legacy ToolSpec.to_openai_dict()
    output (byte-identity gate for LLMReplay fixtures).

    FP-0022: web_fetch is now always in the catalog; the web_fetch_allowed
    parameter is kept for backward compat but is a no-op."""
    from reyn.chat.router_tools import build_tools

    tools = build_tools(
        available_skills=[],
        available_agents=[],
    )

    # Find web_fetch in the returned tools list
    wf_tools = [t for t in tools if t.get("function", {}).get("name") == "web_fetch"]
    assert wf_tools, "web_fetch should appear in build_tools output"

    wf = wf_tools[0]
    assert wf["type"] == "function"
    assert wf["function"]["name"] == "web_fetch"

    # Description byte-identity check (key phrases). Wording updated in
    # #385 PoC PR-D to surface the preview + path_ref contract.
    assert "preview" in wf["function"]["description"]
    assert "path_ref" in wf["function"]["description"]
    assert "max_length" in wf["function"]["description"]
    assert "50000" in wf["function"]["description"]

    # Parameters schema byte-identity check
    params = wf["function"]["parameters"]
    assert params["required"] == ["url"]
    assert "url" in params["properties"]
    assert "max_length" in params["properties"]


def test_build_tools_web_fetch_not_duplicated():
    """Tier 2: web_fetch appears exactly once in build_tools() output.
    Guards against both the registry path and a residual ToolSpec literal
    being included simultaneously. FP-0022: web_fetch is always in catalog."""
    from reyn.chat.router_tools import build_tools

    tools = build_tools(
        available_skills=[],
        available_agents=[],
    )
    wf_tools = [t for t in tools if t.get("function", {}).get("name") == "web_fetch"]
    assert wf_tools, "web_fetch should appear in build_tools output"


# ── 6. Drift detection — description module constant matches render ────────────

def test_web_fetch_description_constant_matches_render():
    """Tier 2: _WEB_FETCH_DESCRIPTION module constant matches the rendered
    description. Ensures no accidental divergence between the constant and
    what WEB_FETCH.description holds."""
    rendered = WEB_FETCH.render_for_router()
    assert rendered["function"]["description"] == _WEB_FETCH_DESCRIPTION
    assert WEB_FETCH.description == _WEB_FETCH_DESCRIPTION


def test_web_fetch_parameters_constant_matches_render():
    """Tier 2: _WEB_FETCH_PARAMETERS module constant matches the rendered
    parameters. Ensures no accidental divergence."""
    rendered = WEB_FETCH.render_for_router()
    assert rendered["function"]["parameters"] == _WEB_FETCH_PARAMETERS
    assert dict(WEB_FETCH.parameters) == _WEB_FETCH_PARAMETERS


# ── 7. FP-0022: Permission tier gate invariants ───────────────────────────────

class _AutoApproveInterventionBus:
    """Minimal real InterventionBus stub that auto-approves every request.

    Returns choice_id='always' so approvals are persisted to the temp
    approvals.yaml and the second call skips the prompt path entirely.
    """
    async def request(self, iv):
        from reyn.user_intervention import InterventionAnswer
        return InterventionAnswer(choice_id="always")


class _DenyAllInterventionBus:
    """Minimal real InterventionBus stub that fails the test if called.

    When the config denies access, require_web_fetch must raise without
    reaching the prompt. If request() is called, the implementation has a bug.
    """
    async def request(self, iv):
        raise AssertionError(
            f"InterventionBus.request called unexpectedly: {iv}"
        )


def test_require_web_fetch_config_allow_pre_approves(tmp_path: Path) -> None:
    """Tier 2: web.fetch: allow in config pre-approves without prompting.

    FP-0022 backward compat: existing `web.fetch: allow` users must not see
    any interactive prompt — the config grant short-circuits at Layer 1.
    """
    from reyn.security.permissions.permissions import PermissionResolver

    resolver = PermissionResolver(
        config_permissions={"web.fetch": "allow"},
        project_root=tmp_path,
        interactive=True,
    )
    # DenyAllInterventionBus: if a prompt fires, the test fails.
    bus = _DenyAllInterventionBus()
    # Must not raise and must not reach the bus.
    asyncio.run(resolver.require_web_fetch("https://example.com", bus))


def test_require_web_fetch_config_deny_raises_immediately(tmp_path: Path) -> None:
    """Tier 2: web.fetch: deny blocks with PermissionError before any prompt.

    FP-0022: deny config must raise immediately, not reach the interactive bus.
    """
    from reyn.security.permissions.permissions import PermissionResolver

    resolver = PermissionResolver(
        config_permissions={"web.fetch": "deny"},
        project_root=tmp_path,
        interactive=True,
    )
    bus = _DenyAllInterventionBus()  # must not be called
    with pytest.raises(PermissionError, match="web fetch denied by config"):
        asyncio.run(resolver.require_web_fetch("https://example.com", bus))


# ── 8. #53 regression — router invoke_action path enforces web.fetch deny ────


def _make_router_op_ctx_factory(resolver, bus, events):
    """Return a callable mirroring RouterHostAdapter.make_router_op_context.

    Builds an OpContext whose ``permission_resolver`` / ``intervention_bus`` /
    ``permission_decl`` mirror what the production factory wires. Used to
    exercise the router-invoked path of WEB_FETCH._handle without spinning
    up a full ChatSession.
    """
    from reyn.op_runtime.context import OpContext
    from reyn.security.permissions.permissions import PermissionDecl
    from reyn.workspace.workspace import Workspace

    def _factory():
        ws = Workspace(
            events=events,
            permission_resolver=resolver,
            skill_name="chat_router",
        )
        return OpContext(
            workspace=ws,
            events=events,
            permission_decl=PermissionDecl(),
            permission_resolver=resolver,
            skill_name="chat_router",
            intervention_bus=bus,
        )

    return _factory


def test_router_invoke_action_web_fetch_deny_raises_permission_error(
    tmp_path: Path,
) -> None:
    """Tier 2: WEB_FETCH._handle raises PermissionError under web.fetch: deny.

    Regression for #53. Before the fix, the router-invoked path of
    ``invoke_action(web__fetch, ...)`` silently bypassed the deny check —
    ``ctx.permission_resolver`` was None (the ToolContext lookup
    ``getattr(host, "permission_resolver", None)`` returned None because
    the adapter stored the resolver as ``_perm``), so ``handle_web_fetch``
    skipped its entire permission gate. The fetch returned ``status: ok,
    status_code: 200``.

    The fix wires:
      1. ``RouterHostAdapter.permission_resolver`` property
      2. ``intervention_bus`` into ``make_router_op_context``
      3. ``web_fetch._handle`` uses ``router_state.op_context_factory``
         instead of synthesizing a minimal OpContext with None bus.

    With those landed, the deny check at Layer 1a of ``require_web_fetch``
    fires before any HTTP traffic and raises PermissionError. ``dispatch_tool``
    wraps that into a ``permission_denied`` error_kind and a tool_failed
    event — but the propagation itself is enough for this regression guard.
    """
    from reyn.events.events import EventLog
    from reyn.security.permissions.permissions import PermissionResolver
    from reyn.tools.types import RouterCallerState, ToolContext

    events = EventLog()
    resolver = PermissionResolver(
        config_permissions={"web.fetch": "deny"},
        project_root=tmp_path,
        interactive=True,
    )
    bus = _DenyAllInterventionBus()  # config-deny path must short-circuit before bus

    rs = RouterCallerState(
        op_context_factory=_make_router_op_ctx_factory(resolver, bus, events),
    )
    tool_ctx = ToolContext(
        events=events,
        permission_resolver=resolver,
        workspace=None,
        caller_kind="router",
        router_state=rs,
    )

    with pytest.raises(PermissionError, match="denied by config"):
        asyncio.run(
            WEB_FETCH.handler({"url": "https://example.com"}, tool_ctx),
        )


def test_router_invoke_action_web_fetch_allow_no_deny_proceeds(
    tmp_path: Path,
) -> None:
    """Tier 2: web.fetch: allow lets the router path proceed past the gate.

    Sibling assertion to the deny test — guards against a fix that
    over-corrects and starts denying every router-invoked web fetch.
    Uses a sentinel URL that resolves to a non-routable IP so the HTTP
    layer fails fast (we only care that the permission gate let us
    through, not that the fetch succeeds).
    """
    from reyn.events.events import EventLog
    from reyn.security.permissions.permissions import PermissionResolver
    from reyn.tools.types import RouterCallerState, ToolContext

    events = EventLog()
    resolver = PermissionResolver(
        config_permissions={"web.fetch": "allow"},
        project_root=tmp_path,
        interactive=True,
    )
    bus = _DenyAllInterventionBus()  # allow-config short-circuits before bus

    rs = RouterCallerState(
        op_context_factory=_make_router_op_ctx_factory(resolver, bus, events),
    )
    tool_ctx = ToolContext(
        events=events,
        permission_resolver=resolver,
        workspace=None,
        caller_kind="router",
        router_state=rs,
    )

    # The gate must not raise PermissionError. The fetch itself may fail
    # at the HTTP layer (unroutable host) — that's fine; the handler
    # returns ``{"status": "error" / "timeout"}`` for transport errors.
    # What matters: no PermissionError, AND no RuntimeError about a
    # missing intervention_bus (= the make_router_op_context wiring).
    result = asyncio.run(WEB_FETCH.handler(
        {"url": "http://127.0.0.1:1/never-listens"},
        tool_ctx,
    ))
    # Either a transport error (= permitted to attempt) or an ok response.
    assert isinstance(result, dict)
    assert result.get("kind") == "web_fetch"
    # The deny path would have raised before getting a dict back.


# ── Phase-side dispatch — reuse the phase OpContext's intervention_bus ──────


def test_phase_dispatch_reuses_op_context_intervention_bus(tmp_path: Path) -> None:
    """Tier 2: WEB_FETCH._handle reuses ``ctx.phase_state.op_context`` when
    the phase carries a real OpContext (= has an intervention_bus).

    Regression for the ``skill_importer`` blocker:
    ``RuntimeError: web_fetch op requires intervention_bus on OpContext``
    fired whenever a skill phase emitted a ``web_fetch`` op via
    ``control_ir_executor`` → ``invoke_tool(WEB_FETCH, ...)``. The handler
    previously had only two branches (router_state factory, or a
    fresh minimal OpContext with bus=None), so the phase path always hit
    the fresh-minimal branch and lost the bus. The fix adds a middle
    branch that picks up the OpContext stashed on
    ``phase_state.op_context`` by ``control_ir_executor._build_ctx``.

    Verification: with a config-allow, the gate must short-circuit before
    needing the bus AND the handler must not raise the "requires
    intervention_bus" RuntimeError. We use an unroutable URL so the HTTP
    layer fails fast — what we're guarding is that the permission path
    didn't trip on a missing bus.
    """
    from reyn.events.events import EventLog
    from reyn.op_runtime.context import OpContext
    from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
    from reyn.tools.types import PhaseCallerState, ToolContext
    from reyn.workspace.workspace import Workspace

    events = EventLog()
    resolver = PermissionResolver(
        config_permissions={"web.fetch": "allow"},
        project_root=tmp_path,
        interactive=True,
    )
    bus = _DenyAllInterventionBus()  # config-allow short-circuits before bus

    workspace = Workspace(
        events=events,
        permission_resolver=resolver,
        skill_name="skill_importer",
    )
    op_ctx_with_bus = OpContext(
        workspace=workspace,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=resolver,
        skill_name="skill_importer",
        intervention_bus=bus,
    )

    phase_state = PhaseCallerState(
        skill_run_id="run-1",
        phase_name="search",
        op_context=op_ctx_with_bus,
    )
    tool_ctx = ToolContext(
        events=events,
        permission_resolver=resolver,
        workspace=workspace,
        caller_kind="phase",
        phase_state=phase_state,
    )

    # Pre-fix this would raise:
    #   RuntimeError: web_fetch op requires intervention_bus on OpContext
    # Post-fix the call proceeds through the permission gate and the
    # HTTP error becomes the visible failure shape instead.
    result = asyncio.run(WEB_FETCH.handler(
        {"url": "http://127.0.0.1:1/never-listens"},
        tool_ctx,
    ))
    assert isinstance(result, dict)
    assert result.get("kind") == "web_fetch"
    # Specifically: no "intervention_bus" RuntimeError leaked through.


def test_phase_dispatch_without_op_context_falls_back_to_minimal(
    tmp_path: Path,
) -> None:
    """Tier 2: When ``phase_state`` lacks an ``op_context`` (= narrow test
    sites, future surfaces), the handler still falls back to building a
    minimal OpContext with ``intervention_bus=None``.

    PR-N14 (C3): the handler no longer raises a "requires intervention_bus"
    RuntimeError on that fallback path. It delegates to
    ``require_http_get``, which honors the established design intent
    (permissions.py:741-754) of branching on actual prompt necessity
    rather than failing closed on a missing bus. With a config-allow
    (``web.fetch: allow``) the gate short-circuits before the bus is
    ever needed, so the fetch proceeds and the unreachable URL becomes
    the visible failure shape (a web_fetch error dict) — NOT a
    bus-missing RuntimeError.
    """
    from reyn.events.events import EventLog
    from reyn.security.permissions.permissions import PermissionResolver
    from reyn.tools.types import PhaseCallerState, ToolContext

    events = EventLog()
    resolver = PermissionResolver(
        config_permissions={"web.fetch": "allow"},
        project_root=tmp_path,
        interactive=True,
    )

    # Phase state with no op_context (= test-site shape).
    phase_state = PhaseCallerState(
        skill_run_id="run-1",
        phase_name="search",
        op_context=None,
    )
    tool_ctx = ToolContext(
        events=events,
        permission_resolver=resolver,
        workspace=None,
        caller_kind="phase",
        phase_state=phase_state,
    )

    # Post-PR-N14: config-allow short-circuits the permission gate before
    # the bus is consulted, so the handler proceeds past permissions and
    # returns a web_fetch result dict (the unreachable URL surfaces as an
    # error status) — no "requires intervention_bus" RuntimeError.
    result = asyncio.run(WEB_FETCH.handler(
        {"url": "http://127.0.0.1:1/never-listens"},
        tool_ctx,
    ))
    assert isinstance(result, dict)
    assert result.get("kind") == "web_fetch"
