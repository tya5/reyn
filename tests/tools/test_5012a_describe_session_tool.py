"""Tier 2: `describe_session` — a REAL callable-as-tool path, not just a
value-returning op (#5012-A).

lead-coder's acceptance criterion for #5012-A (broker, 2026-08-21): "an op
that returns a value is not enough — one witness that a callable-as-tool
path exists." A test of `op_runtime.describe_session.handle` alone (already
covered by exercising the op directly, e.g. `test_compact_op_272.py`'s
shape) proves the OP works; it does not prove the LLM can actually reach it
— that requires the REAL registry entry, the REAL ToolDefinition.handler,
and (for the second test below) the REAL production `OpContext` assembly
chain (`Session` -> `RouterHostAdapter.make_router_op_context` ->
`RouterOpContextSource.build` -> `build_router_op_context`), not a
hand-built stand-in `OpContext`.

No mocks: real `ToolRegistry`, real `Session` (via `make_session`, the
project's own Agent-identity-SSoT test helper), real `RouterHostAdapter`.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.config.infra import AuthConfig, OAuthProviderConfig, SandboxConfig
from reyn.core.events.events import EventLog
from reyn.data.workspace.workspace import Workspace
from reyn.tools import get_default_registry
from reyn.tools.types import RouterCallerState, ToolContext
from tests._support.agent_session import make_session

# ── 1. Registry surface — the tool is actually registered and dispatchable ──


def test_describe_session_is_registered_router_allow_discovery():
    """Tier 2: describe_session is in the default registry, router-visible,
    read-only, and filed under discovery (mirrors search_actions/describe_action)."""
    registry = get_default_registry()
    td = registry.lookup("describe_session")
    assert td.name == "describe_session"
    assert td.gates.router == "allow"
    assert td.purity == "read_only"
    assert td.category == "discovery"
    assert "describe_session" in [t.name for t in registry.for_router()]


def test_describe_session_tool_handler_dispatches_through_real_op_runtime():
    """Tier 2: calling the REGISTERED ToolDefinition's handler — the exact
    call RouterLoop makes on a tool_call — runs the real op and returns the
    3-field report, via the bridge's minimal-fallback OpContext path (no
    router_state — the ADR-0026 Open Q #7 test-site path)."""
    events = EventLog()
    ws = Workspace(events=events, permission_resolver=None, actor="test", base_dir=Path.cwd())
    ctx = ToolContext(events=events, permission_resolver=None, workspace=ws, caller_kind="router")

    registry = get_default_registry()
    td = registry.lookup("describe_session")
    result = asyncio.run(td.handler({}, ctx))

    assert result["kind"] == "describe_session"
    assert result["status"] == "ok"
    assert set(result["write_scope"]) >= {"declared"}
    assert set(result["position"]) >= {"repo_root", "branch", "head", "capability"}
    assert isinstance(result["auth_status"], dict)


# ── 2. Production wiring — the SAME OpContext a real chat turn would get ────


def test_describe_session_reaches_real_router_op_context_wiring():
    """Tier 2: the SAME auth_config/sandbox_config a real Session was
    constructed with reach describe_session's result through the actual
    production assembly chain — Session -> RouterHostAdapter.
    make_router_op_context -> RouterOpContextSource.build ->
    build_router_op_context -> OpContext.{auth_config,sandbox_config} — not
    a hand-built OpContext standing in for it.

    FALSIFY: if #5012-A's OpContext.auth_config field, or its threading
    through build_router_op_context/RouterOpContextSource/Session, were
    dropped, ``result["write_scope"]["declared"]`` would read False instead
    of True and ``result["auth_status"]`` would come back empty instead of
    naming "github" — this test would fail for the right reason."""
    sandbox_config = SandboxConfig(policy={"allow_write_paths": ["src/"], "deny_write_paths": []})
    auth_config = AuthConfig(
        providers={
            "github": OAuthProviderConfig(
                name="github",
                client_id="test-client-id",
                device_authorization_url="https://example.invalid/device/code",
                token_url="https://example.invalid/oauth/token",
            ),
        },
    )

    session = make_session(
        agent_name="describe-session-witness",
        sandbox_config=sandbox_config,
        auth_config=auth_config,
    )

    # RouterLoop's real `_build_router_caller_state` binds op_context_factory
    # to exactly this method (see tools/types.py:305) — reproduced here so
    # the ToolDefinition's PREFERRED bridge branch (build_legacy_op_context's
    # own docstring calls it out as "production, ADR-0026 Phase 3.5") is what
    # actually runs, not the test-site fallback.
    router_state = RouterCallerState(
        op_context_factory=session.router_host.make_router_op_context,
    )
    events = EventLog()
    ctx = ToolContext(
        events=events,
        permission_resolver=None,
        workspace=session.router_host.make_router_op_context().workspace,
        caller_kind="router",
        router_state=router_state,
    )

    registry = get_default_registry()
    td = registry.lookup("describe_session")
    result = asyncio.run(td.handler({}, ctx))

    assert result["write_scope"]["declared"] is True
    assert result["write_scope"]["allow_write_paths"] == ["src/"]
    # "github" is present because it's what THIS test's auth_config declared —
    # authenticated True/False depends on this machine's real OAuth token
    # store (session_auth_status's own isolated-tmp_path tests pin that
    # discriminator; here only reachability is under test), so only the key
    # + its shape are asserted, never the boolean.
    assert "github" in result["auth_status"]
    assert set(result["auth_status"]["github"]) == {"authenticated", "reason"}
