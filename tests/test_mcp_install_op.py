"""Tier 2: mcp_install IR op handler invariants.

Tests the OS-level contract of the mcp_install op:
  - permission gate path (no file.write declaration → PermissionError)
  - runtime hint check (missing runtime → error result)
  - secrets persistence: save_secret called for isSecret env vars
  - reyn.yaml written with ${KEY} reference (not raw value)
  - scope tier: local → reyn.local.yaml, project → reyn.yaml, user → ~/.reyn/config.yaml
  - mcp_server_installed event emitted (server_id / scope / runtime / env_keys_set present;
    secret values NOT in the event)

#571 collapse arc Phase 5: the bool-axis ``mcp_install`` was removed.
Tests now use ``_phase5_install_decl(resolver)`` which builds the
explicit ``file.write`` + ``http.get`` decl and session-approves the
canonical path.

No mocks of real collaborators. RegistryClient._get is patched at the HTTP layer
(same pattern as test_mcp_search_registry.py) to avoid network calls.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import mock

import pytest

from reyn.events.events import EventLog
from reyn.op_runtime.context import OpContext
from reyn.op_runtime.mcp_install import handle as mcp_install_handle
from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.schemas.models import MCPInstallIROp
from reyn.user_intervention import InterventionAnswer, UserIntervention

# ---------------------------------------------------------------------------
# Fixtures: fake server.json responses
# ---------------------------------------------------------------------------

_FILESYSTEM_SERVER_RESPONSE = {
    "name": "io.github.modelcontextprotocol/server-filesystem",
    "description": "Filesystem MCP server",
    "version": "0.6.2",
    "repository": {"url": "https://github.com/modelcontextprotocol/servers"},
    "$schema": "https://static.modelcontextprotocol.io/schemas/server.schema.json",
    "packages": [
        {
            "registryType": "npm",
            "identifier": "@modelcontextprotocol/server-filesystem",
            "version": "0.6.2",
            "transport": {"type": "stdio"},
            "environmentVariables": [],
        }
    ],
    "remotes": [],
}

_SECRET_SERVER_RESPONSE = {
    "name": "io.github.example/secret-server",
    "description": "Server requiring a secret",
    "version": "1.0.0",
    "repository": {"url": "https://github.com/example/secret-server"},
    "$schema": "https://static.modelcontextprotocol.io/schemas/server.schema.json",
    "packages": [
        {
            "registryType": "npm",
            "identifier": "@example/secret-server",
            "version": "1.0.0",
            "transport": {"type": "stdio"},
            "environmentVariables": [
                {
                    "name": "EXAMPLE_API_KEY",
                    "description": "API key for example service",
                    "isSecret": True,
                }
            ],
        }
    ],
    "remotes": [],
}

_PYPI_SERVER_RESPONSE = {
    "name": "io.pypi/my-mcp-server",
    "description": "Python MCP server",
    "version": "2.0.0",
    "repository": {"url": "https://github.com/example/my-mcp-server"},
    "$schema": "https://static.modelcontextprotocol.io/schemas/server.schema.json",
    "packages": [
        {
            "registryType": "pypi",
            "identifier": "my-mcp-server",
            "version": "2.0.0",
            "transport": {"type": "stdio"},
            "environmentVariables": [],
        }
    ],
    "remotes": [],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _AutoApproveInterventionBus:
    """Real InterventionBus that auto-approves all prompts.

    For permission prompts: returns choice_id="always".
    For secret text prompts (choices=[]) : returns text="test-secret-value".
    """

    def __init__(self) -> None:
        self.requests: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.requests.append(iv)
        if iv.choices:
            return InterventionAnswer(choice_id="always")
        # Free-text prompt (secret value)
        return InterventionAnswer(text="test-secret-value")


class _DenyInterventionBus:
    """Real InterventionBus that denies all permission prompts."""

    def __init__(self) -> None:
        self.requests: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.requests.append(iv)
        return InterventionAnswer(choice_id="no")


def _make_resolver(tmp_path: Path, *, config: dict | None = None) -> PermissionResolver:
    return PermissionResolver(
        config_permissions=config or {},
        project_root=tmp_path,
        interactive=True,
    )


def _phase5_install_decl(resolver: PermissionResolver) -> PermissionDecl:
    """Phase 5 + Phase 6 successor to ``PermissionDecl(mcp_install=True)``.

    Builds the explicit list-axis decl the op handler now consumes:
    ``file.write`` on the canonical config path + ``http.get`` for the
    registry host + wildcard ``secret.write`` (= #571 Phase 6,
    authorises save_secret for runtime-determined env-var keys from
    the registry response). Session-approves the file path so the
    ``require_file_write`` check passes without an interactive prompt.
    """
    canonical_config = str(resolver._project_root / ".reyn" / "mcp.yaml")
    resolver.session_approve_path(canonical_config, "mcp_install_test", "file.write")
    return PermissionDecl(
        file_write=[{"path": canonical_config, "scope": "just_path"}],
        http_get=[{"host": "registry.modelcontextprotocol.io"}],
        secret_write=["*"],
    )


def _make_op_ctx(
    tmp_path: Path,
    resolver: PermissionResolver,
    bus: object,
    decl: PermissionDecl,
) -> OpContext:
    events = EventLog()
    # Minimal workspace stub with .root attribute for config path resolution
    workspace = type("Workspace", (), {"root": str(tmp_path)})()
    return OpContext(
        workspace=workspace,
        events=events,
        permission_decl=decl,
        permission_resolver=resolver,
        skill_name="mcp_install_test",
        intervention_bus=bus,
    )


def _patch_registry_get(server_response: dict, status: int = 200):
    """Patch RegistryClient._get to return a fixed response."""

    async def _fake_get(self, path: str, params=None):
        if status >= 400:
            from reyn.registry.client import RegistryError
            raise RegistryError(f"HTTP {status}")
        # get_server wraps with {"server": ...} for the versions/latest endpoint
        return {"server": server_response}

    return mock.patch("reyn.registry.client.RegistryClient._get", _fake_get)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tier 2: permission gate path
# ---------------------------------------------------------------------------


def test_permission_gate_undeclared_raises(tmp_path):
    """Tier 2: mcp_install op raises PermissionError when file.write not declared.

    #571 collapse arc Phase 5: the legacy ``mcp_install: true`` bool
    axis was removed; the op handler now gates via
    ``require_file_write`` on ``.reyn/mcp.yaml``. A skill that
    declares neither the explicit file.write entry nor the (now
    removed) bool axis fails the gate.
    """
    resolver = _make_resolver(tmp_path)
    decl = PermissionDecl()  # nothing declared
    bus = _AutoApproveInterventionBus()
    ctx = _make_op_ctx(tmp_path, resolver, bus, decl)

    op = MCPInstallIROp(kind="mcp_install", server_id="io.github.example/server-x")

    with _patch_registry_get(_FILESYSTEM_SERVER_RESPONSE):
        with pytest.raises(PermissionError, match="not approved"):
            _run(mcp_install_handle(op, ctx, "control_ir"))


def test_permission_gate_passes_with_explicit_decl(tmp_path):
    """Tier 2: mcp_install op proceeds when explicit file.write + http.get are declared."""
    resolver = _make_resolver(tmp_path)
    decl = _phase5_install_decl(resolver)
    bus = _AutoApproveInterventionBus()
    ctx = _make_op_ctx(tmp_path, resolver, bus, decl)

    op = MCPInstallIROp(
        kind="mcp_install",
        server_id="io.github.modelcontextprotocol/server-filesystem",
        scope="local",
    )

    with mock.patch("shutil.which", return_value="/usr/bin/npx"):
        with _patch_registry_get(_FILESYSTEM_SERVER_RESPONSE):
            result = _run(mcp_install_handle(op, ctx, "control_ir"))

    assert result["status"] == "ok"
    assert result["server_id"] == "io.github.modelcontextprotocol/server-filesystem"


# ---------------------------------------------------------------------------
# Tier 2: runtimeHint check
# ---------------------------------------------------------------------------


def test_missing_runtime_returns_error(tmp_path):
    """Tier 2: Handler returns error result when runtime command is not found.

    npx missing → status="error" with install hint, no exception raised.
    """
    resolver = _make_resolver(tmp_path, config={"mcp_install": "allow"})
    decl = _phase5_install_decl(resolver)
    bus = _AutoApproveInterventionBus()
    ctx = _make_op_ctx(tmp_path, resolver, bus, decl)

    op = MCPInstallIROp(
        kind="mcp_install",
        server_id="io.github.modelcontextprotocol/server-filesystem",
        scope="local",
    )

    with mock.patch("shutil.which", return_value=None):
        with _patch_registry_get(_FILESYSTEM_SERVER_RESPONSE):
            result = _run(mcp_install_handle(op, ctx, "control_ir"))

    assert result["status"] == "error"
    assert "npx" in result["error"].lower() or "node" in result["error"].lower()


# ---------------------------------------------------------------------------
# Tier 2: secrets — env_overrides skip prompt; values not in event
# ---------------------------------------------------------------------------


def test_env_overrides_skip_prompt_and_persist_secret(tmp_path):
    """Tier 2: env_overrides pre-supply secret values, skipping interactive prompt.

    The secret is persisted via secrets.store and the ${KEY} reference is
    written to the config file — not the raw value.
    """
    resolver = _make_resolver(tmp_path, config={"mcp_install": "allow"})
    decl = _phase5_install_decl(resolver)
    bus = _AutoApproveInterventionBus()
    ctx = _make_op_ctx(tmp_path, resolver, bus, decl)

    secrets_path = tmp_path / ".reyn" / "secrets.env"

    op = MCPInstallIROp(
        kind="mcp_install",
        server_id="io.github.example/secret-server",
        scope="local",
        env_overrides={"EXAMPLE_API_KEY": "my-secret-value"},
    )

    with mock.patch("shutil.which", return_value="/usr/bin/npx"):
        with mock.patch(
            "reyn.secrets.store._secrets_path", return_value=secrets_path
        ):
            with _patch_registry_get(_SECRET_SERVER_RESPONSE):
                result = _run(mcp_install_handle(op, ctx, "control_ir"))

    assert result["status"] == "ok"
    assert "EXAMPLE_API_KEY" in result["env_keys_set"]

    # Secret was persisted
    assert secrets_path.exists()
    secrets_text = secrets_path.read_text(encoding="utf-8")
    assert "EXAMPLE_API_KEY" in secrets_text
    assert "my-secret-value" in secrets_text

    # Config file written with ${KEY} reference, not raw value.
    # Issue #470 (2026-05-22): write target is now ``.reyn/mcp.yaml``
    # regardless of the op's ``scope`` arg — separating dynamic MCP
    # registry from static deployment config.
    config_path = tmp_path / ".reyn" / "mcp.yaml"
    assert config_path.exists()
    import yaml
    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    servers = written["mcp"]["servers"]
    server_name = result["server_name"]
    env_section = servers[server_name]["env"]
    assert env_section.get("EXAMPLE_API_KEY") == "${EXAMPLE_API_KEY}"
    # Raw value must not appear in config
    assert "my-secret-value" not in config_path.read_text(encoding="utf-8")


def test_save_secret_blocked_when_secret_write_not_declared(tmp_path):
    """Tier 2: mcp_install raises PermissionError when secret.write is missing.

    #571 Phase 6: every save_secret call routes through
    require_secret_write. A skill that declares file.write + http.get
    but forgets secret.write fails the gate when the registry server
    has ``isSecret`` env vars.
    """
    resolver = _make_resolver(tmp_path)
    canonical_config = str(resolver._project_root / ".reyn" / "mcp.yaml")
    resolver.session_approve_path(canonical_config, "mcp_install_test", "file.write")
    decl = PermissionDecl(
        file_write=[{"path": canonical_config, "scope": "just_path"}],
        http_get=[{"host": "registry.modelcontextprotocol.io"}],
        # secret_write omitted — gate fires when first secret save is attempted
    )
    bus = _AutoApproveInterventionBus()
    ctx = _make_op_ctx(tmp_path, resolver, bus, decl)
    secrets_path = tmp_path / ".reyn" / "secrets.env"

    op = MCPInstallIROp(
        kind="mcp_install",
        server_id="io.github.example/secret-server",
        scope="local",
        env_overrides={"EXAMPLE_API_KEY": "my-secret-value"},
    )

    with mock.patch("shutil.which", return_value="/usr/bin/npx"):
        with mock.patch(
            "reyn.secrets.store._secrets_path", return_value=secrets_path
        ):
            with _patch_registry_get(_SECRET_SERVER_RESPONSE):
                with pytest.raises(PermissionError, match="EXAMPLE_API_KEY"):
                    _run(mcp_install_handle(op, ctx, "control_ir"))


def test_secret_value_not_in_event(tmp_path):
    """Tier 2: mcp_server_installed event contains env_keys_set names but not values.

    P6 audit trail must never leak credential values into the event log.
    """
    resolver = _make_resolver(tmp_path, config={"mcp_install": "allow"})
    decl = _phase5_install_decl(resolver)
    bus = _AutoApproveInterventionBus()
    ctx = _make_op_ctx(tmp_path, resolver, bus, decl)
    secrets_path = tmp_path / ".reyn" / "secrets.env"

    op = MCPInstallIROp(
        kind="mcp_install",
        server_id="io.github.example/secret-server",
        scope="local",
        env_overrides={"EXAMPLE_API_KEY": "super-secret-123"},
    )

    with mock.patch("shutil.which", return_value="/usr/bin/npx"):
        with mock.patch(
            "reyn.secrets.store._secrets_path", return_value=secrets_path
        ):
            with _patch_registry_get(_SECRET_SERVER_RESPONSE):
                _run(mcp_install_handle(op, ctx, "control_ir"))

    events = ctx.events.all()
    install_events = [e for e in events if e.type == "mcp_server_installed"]
    assert install_events, "Expected mcp_server_installed event"

    evt = install_events[0]
    # Key name should be present
    assert "EXAMPLE_API_KEY" in evt.data.get("env_keys_set", [])
    # Secret value must NOT be in any event data
    event_str = str(evt.data)
    assert "super-secret-123" not in event_str


# ---------------------------------------------------------------------------
# Tier 2: scope tier — file written to correct path
# ---------------------------------------------------------------------------


def test_install_writes_to_dynamic_mcp_yaml_regardless_of_scope(tmp_path):
    """Tier 2 (#470 2026-05-22 contract reversal): scope arg is now a
    no-op — every install writes to ``.reyn/mcp.yaml`` regardless of
    whether the LLM passes ``scope="local"`` / ``"project"`` / ``"user"``
    / nothing. The architectural decision is "one canonical dynamic
    registry location" (= separates op-mutated MCP servers from
    static deployment config in reyn.yaml).

    Renamed + consolidated from three prior tests
    (test_scope_local_writes_to_reyn_local_yaml /
    test_scope_project_writes_to_reyn_yaml /
    test_scope_user_writes_to_home_reyn_config) that pinned the
    pre-#470 per-scope routing. Per ``feedback_contract_reversal_rewrites_tests``,
    the rewrite (not patch) makes the contract change visible at
    review.
    """
    for scope in ("local", "project", "user"):
        resolver = _make_resolver(tmp_path, config={"mcp_install": "allow"})
        decl = _phase5_install_decl(resolver)
        bus = _AutoApproveInterventionBus()
        ctx = _make_op_ctx(tmp_path, resolver, bus, decl)

        op = MCPInstallIROp(
            kind="mcp_install",
            server_id="io.github.modelcontextprotocol/server-filesystem",
            scope=scope,
        )

        with mock.patch("shutil.which", return_value="/usr/bin/npx"):
            with _patch_registry_get(_FILESYSTEM_SERVER_RESPONSE):
                result = _run(mcp_install_handle(op, ctx, "control_ir"))

        expected_path = tmp_path / ".reyn" / "mcp.yaml"
        assert result["status"] == "ok"
        assert result["installed_path"] == str(expected_path), (
            f"scope={scope!r} should write to .reyn/mcp.yaml; got {result['installed_path']}"
        )
        assert expected_path.exists()

        # The minted shape carries the ``{mcp: {servers: {...}}}`` wrapper
        # so the existing config loader's ``_merge`` handles it without
        # special-casing.
        import yaml
        written = yaml.safe_load(expected_path.read_text(encoding="utf-8"))
        assert "mcp" in written
        assert "servers" in written["mcp"]
        # Clean up between iterations so subsequent scope tests start fresh.
        expected_path.unlink()


# ---------------------------------------------------------------------------
# Tier 2: mcp_server_installed event emitted (P6)
# ---------------------------------------------------------------------------


def test_event_emitted_on_success(tmp_path):
    """Tier 2: mcp_server_installed event is emitted on successful install.

    Event must carry server_id, scope, runtime, env_keys_set.
    """
    resolver = _make_resolver(tmp_path, config={"mcp_install": "allow"})
    decl = _phase5_install_decl(resolver)
    bus = _AutoApproveInterventionBus()
    ctx = _make_op_ctx(tmp_path, resolver, bus, decl)

    op = MCPInstallIROp(
        kind="mcp_install",
        server_id="io.github.modelcontextprotocol/server-filesystem",
        scope="local",
    )

    with mock.patch("shutil.which", return_value="/usr/bin/npx"):
        with _patch_registry_get(_FILESYSTEM_SERVER_RESPONSE):
            result = _run(mcp_install_handle(op, ctx, "control_ir"))

    assert result["status"] == "ok"

    events = ctx.events.all()
    install_events = [e for e in events if e.type == "mcp_server_installed"]
    assert len(install_events) == 1

    evt = install_events[0]
    assert evt.data["server_id"] == "io.github.modelcontextprotocol/server-filesystem"
    assert evt.data["scope"] == "local"
    assert "runtime" in evt.data
    assert "env_keys_set" in evt.data
    assert isinstance(evt.data["env_keys_set"], list)


def test_no_event_on_permission_denied(tmp_path):
    """Tier 2: No mcp_server_installed event when permission gate rejects.

    The event is only emitted after a successful install (P6 — only emit on
    actual state change). #571 Phase 5: the rejection now comes from
    ``require_file_write`` when ``.reyn/mcp.yaml`` is not declared.
    """
    resolver = _make_resolver(tmp_path)
    decl = PermissionDecl()  # missing required file.write declaration
    bus = _AutoApproveInterventionBus()
    ctx = _make_op_ctx(tmp_path, resolver, bus, decl)

    op = MCPInstallIROp(
        kind="mcp_install",
        server_id="io.github.example/server-x",
        scope="local",
    )

    with _patch_registry_get(_FILESYSTEM_SERVER_RESPONSE):
        with pytest.raises(PermissionError):
            _run(mcp_install_handle(op, ctx, "control_ir"))

    install_events = [e for e in ctx.events.all() if e.type == "mcp_server_installed"]
    assert install_events == []


# ---------------------------------------------------------------------------
# Tier 2: registry fetch failure
# ---------------------------------------------------------------------------


def test_registry_fetch_failure_returns_error(tmp_path):
    """Tier 2: Registry fetch failure returns error result without raising.

    The op handler must return a structured error dict (not propagate the
    RegistryError), so the OS can surface it to the LLM as a control_ir_result.
    """
    resolver = _make_resolver(tmp_path, config={"mcp_install": "allow"})
    decl = _phase5_install_decl(resolver)
    bus = _AutoApproveInterventionBus()
    ctx = _make_op_ctx(tmp_path, resolver, bus, decl)

    op = MCPInstallIROp(
        kind="mcp_install",
        server_id="io.github.example/nonexistent",
        scope="local",
    )

    with _patch_registry_get({}, status=404):
        result = _run(mcp_install_handle(op, ctx, "control_ir"))

    assert result["status"] == "error"
    assert "Registry" in result["error"] or "registry" in result["error"].lower()


# ---------------------------------------------------------------------------
# Tier 2: MCPInstallIROp model
# ---------------------------------------------------------------------------


def test_mcp_install_irop_model_defaults():
    """Tier 2: MCPInstallIROp has correct defaults and required fields."""
    op = MCPInstallIROp(kind="mcp_install", server_id="io.example/server")
    assert op.scope == "local"
    assert op.env_overrides is None


def test_mcp_install_irop_in_op_kind_model_map():
    """Tier 2: mcp_install is registered in OP_KIND_MODEL_MAP."""
    from reyn.op_runtime.registry import OP_KIND_MODEL_MAP
    from reyn.schemas.models import MCPInstallIROp

    assert "mcp_install" in OP_KIND_MODEL_MAP
    assert OP_KIND_MODEL_MAP["mcp_install"] is MCPInstallIROp


def test_mcp_install_in_all_op_kinds():
    """Tier 2: mcp_install is in ALL_OP_KINDS (used by DSL linter)."""
    from reyn.op_runtime.registry import ALL_OP_KINDS

    assert "mcp_install" in ALL_OP_KINDS


def test_mcp_install_op_tool_definition_gates():
    """Tier 2: MCP_INSTALL_OP ToolDefinition has router=deny, phase=allow."""
    from reyn.tools.mcp_install import MCP_INSTALL_OP

    assert MCP_INSTALL_OP.gates.router == "deny"
    assert MCP_INSTALL_OP.gates.phase == "allow"


def test_mcp_install_op_registered_in_default_registry():
    """Tier 2: mcp_install is present in get_default_registry() with phase=allow."""
    from reyn.tools import get_default_registry

    registry = get_default_registry()
    tool = registry.lookup("mcp_install")
    assert tool is not None
    assert tool.gates.phase == "allow"
    assert tool.gates.router == "deny"
