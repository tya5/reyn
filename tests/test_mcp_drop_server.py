"""Tier 2: FP-0034 PR-4 mcp_drop_server op + ToolDefinition + dispatch route.

Tests for:
  1. PermissionResolver.require_mcp_drop_server (FP-0034 §D23 gate)
  2. op_runtime/mcp_drop_server.py — yaml edit, secrets cleanup,
     scope auto-detect, not-found path, P6 event emission
  3. tools/mcp_drop.py ToolDefinition shape + registration
  4. universal_dispatch route for ``mcp.operation__drop_server``

No mocks of collaborators. Uses real PermissionResolver +
InterventionBus stand-in (= records calls + returns canned answer).
Filesystem fixtures use tmp_path so secret writes / yaml writes
never touch the user environment.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.schemas.models import MCPDropServerIROp
from reyn.user_intervention import (
    InterventionAnswer,
    InterventionBus,
    UserIntervention,
)


# ── Shared helpers ────────────────────────────────────────────────────────


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _RecordingBus:
    """Real InterventionBus implementation that answers a canned choice.

    Mirrors the pattern in tests/test_permissions_mcp_install.py:_RecordingBus.
    """

    def __init__(self, answer_choice: str = "no") -> None:
        self.requests: list[UserIntervention] = []
        self.answer_choice = answer_choice

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.requests.append(iv)
        return InterventionAnswer(choice_id=self.answer_choice)


def _resolver(
    tmp_path: Path,
    *,
    config: dict | None = None,
    interactive: bool = False,
) -> PermissionResolver:
    return PermissionResolver(
        config_permissions=config or {},
        project_root=tmp_path,
        interactive=interactive,
    )


class _CapturingEvents:
    """Captures emit() calls for P6 assertion."""

    subscribers: list[Any] = []

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, name: str, **kwargs: Any) -> None:
        self.events.append((name, kwargs))


# ── 1. Permission gate — require_mcp_drop_server ─────────────────────────


def test_require_mcp_drop_server_decl_guard(tmp_path: Path) -> None:
    """Tier 2: decl.mcp_drop_server=False raises immediately.

    Install intent alone (mcp_install=True) is NOT sufficient — drop
    is a distinct gate per §D23.
    """
    resolver = _resolver(tmp_path)
    decl = PermissionDecl(mcp_install=True, mcp_drop_server=False)
    bus = _RecordingBus()

    with pytest.raises(PermissionError, match="not declared"):
        _run(resolver.require_mcp_drop_server(decl, "filesystem", bus))

    # Prompt was NOT shown — decl guard fires before bus is consulted.
    assert bus.requests == []


def test_require_mcp_drop_server_config_deny(tmp_path: Path) -> None:
    """Tier 2: permissions.mcp_drop_server: deny raises after decl guard."""
    resolver = _resolver(tmp_path, config={"mcp_drop_server": "deny"})
    decl = PermissionDecl(mcp_drop_server=True)
    bus = _RecordingBus()

    with pytest.raises(PermissionError, match="denied by config"):
        _run(resolver.require_mcp_drop_server(decl, "filesystem", bus))

    assert bus.requests == []


def test_require_mcp_drop_server_config_allow(tmp_path: Path) -> None:
    """Tier 2: permissions.mcp_drop_server: allow passes without prompting."""
    resolver = _resolver(tmp_path, config={"mcp_drop_server": "allow"})
    decl = PermissionDecl(mcp_drop_server=True)
    bus = _RecordingBus()

    _run(resolver.require_mcp_drop_server(decl, "filesystem", bus))

    assert bus.requests == []


def test_require_mcp_drop_server_interactive_approval(tmp_path: Path) -> None:
    """Tier 2: ask default path triggers interactive prompt; 'yes' approves."""
    resolver = _resolver(tmp_path, interactive=True)
    decl = PermissionDecl(mcp_drop_server=True)
    bus = _RecordingBus(answer_choice="yes")

    _run(resolver.require_mcp_drop_server(decl, "filesystem", bus))

    assert len(bus.requests) == 1


def test_require_mcp_drop_server_interactive_denial(tmp_path: Path) -> None:
    """Tier 2: ask default path raises when user denies."""
    resolver = _resolver(tmp_path, interactive=True)
    decl = PermissionDecl(mcp_drop_server=True)
    bus = _RecordingBus(answer_choice="no")

    with pytest.raises(PermissionError, match="denied by user"):
        _run(resolver.require_mcp_drop_server(decl, "filesystem", bus))

    assert len(bus.requests) == 1


def test_require_mcp_drop_server_auto_approve_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: REYN_MCP_DROP_SERVER_AUTO_APPROVE=1 skips the prompt."""
    monkeypatch.setenv("REYN_MCP_DROP_SERVER_AUTO_APPROVE", "1")
    resolver = _resolver(tmp_path)
    decl = PermissionDecl(mcp_drop_server=True)
    bus = _RecordingBus(answer_choice="no")  # would deny if reached

    _run(resolver.require_mcp_drop_server(decl, "filesystem", bus))

    assert bus.requests == []


def test_permission_decl_from_dict_parses_mcp_drop_server() -> None:
    """Tier 2: PermissionDecl.from_dict round-trips mcp_drop_server field."""
    decl = PermissionDecl.from_dict({"mcp_drop_server": True})
    assert decl.mcp_drop_server is True

    decl2 = PermissionDecl.from_dict({})  # default
    assert decl2.mcp_drop_server is False


# ── 2. op_runtime handler — yaml edit ─────────────────────────────────────


def _seed_config(path: Path, servers: dict[str, dict]) -> None:
    """Write a reyn.local.yaml-shape config with the given servers block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump({"mcp": {"servers": servers}}, allow_unicode=True),
        encoding="utf-8",
    )


class _StubWorkspace:
    """Workspace stand-in exposing only ``base_dir`` (= what the handler reads).

    The real Workspace class reads CWD eagerly + creates `.reyn/`
    directories; using it in unit tests pollutes the test filesystem.
    The handler only consults ``ws.base_dir`` via
    ``_resolve_project_root`` so a stub is sufficient + cleaner.
    """

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir


def _make_op_ctx(
    tmp_path: Path,
    *,
    permission_decl: PermissionDecl | None = None,
    resolver: PermissionResolver | None = None,
    bus: InterventionBus | None = None,
    events: _CapturingEvents | None = None,
) -> Any:
    """Build a minimal OpContext for op_runtime handler tests."""
    from reyn.op_runtime.context import OpContext

    return OpContext(
        workspace=_StubWorkspace(base_dir=tmp_path),
        events=events or _CapturingEvents(),
        permission_decl=permission_decl or PermissionDecl(mcp_drop_server=True),
        permission_resolver=resolver,
        skill_name="",
        intervention_bus=bus,
        subscribers=[],
    )


def test_mcp_drop_server_removes_entry_local_scope(tmp_path: Path) -> None:
    """Tier 2: explicit local scope removes the entry from reyn.local.yaml."""
    from reyn.op_runtime.mcp_drop_server import handle as drop_handle

    cfg_path = tmp_path / "reyn.local.yaml"
    _seed_config(cfg_path, {
        "filesystem": {"command": "npx", "args": ["-y", "@mcp/fs"]},
        "brave":      {"command": "uvx", "args": ["brave-mcp"]},
    })

    op = MCPDropServerIROp(
        kind="mcp_drop_server",
        server="filesystem",
        scope="local",
        clear_secrets=False,
    )
    result = _run(drop_handle(op=op, ctx=_make_op_ctx(tmp_path), caller="control_ir"))

    assert result["status"] == "ok"
    assert result["server"] == "filesystem"
    assert result["scope"] == "local"

    # Reload and verify yaml shape
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert "filesystem" not in data["mcp"]["servers"]
    assert "brave" in data["mcp"]["servers"]


def test_mcp_drop_server_auto_detects_scope(tmp_path: Path) -> None:
    """Tier 2: scope=None walks local → project → user and removes from first match."""
    from reyn.op_runtime.mcp_drop_server import handle as drop_handle

    # Server lives in project, not local
    _seed_config(tmp_path / "reyn.local.yaml", {"other": {}})
    _seed_config(tmp_path / "reyn.yaml", {"filesystem": {"command": "npx"}})

    op = MCPDropServerIROp(
        kind="mcp_drop_server",
        server="filesystem",
        scope=None,
        clear_secrets=False,
    )
    result = _run(drop_handle(op=op, ctx=_make_op_ctx(tmp_path), caller="control_ir"))

    assert result["status"] == "ok"
    assert result["scope"] == "project"

    project_data = yaml.safe_load((tmp_path / "reyn.yaml").read_text(encoding="utf-8"))
    # Pruned to empty → mcp/servers/mcp blocks removed
    assert "mcp" not in project_data or "filesystem" not in project_data.get(
        "mcp", {},
    ).get("servers", {})


def test_mcp_drop_server_prunes_empty_containers(tmp_path: Path) -> None:
    """Tier 2: when removing the last server, mcp.servers and mcp are pruned."""
    from reyn.op_runtime.mcp_drop_server import handle as drop_handle

    cfg_path = tmp_path / "reyn.local.yaml"
    _seed_config(cfg_path, {"filesystem": {"command": "npx"}})

    op = MCPDropServerIROp(
        kind="mcp_drop_server", server="filesystem", scope="local",
        clear_secrets=False,
    )
    _run(drop_handle(op=op, ctx=_make_op_ctx(tmp_path), caller="control_ir"))

    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    # Either entire mcp block gone, or none of its sub-keys remain.
    assert ("mcp" not in (data or {})) or (data["mcp"] == {})


def test_mcp_drop_server_not_found_in_explicit_scope(tmp_path: Path) -> None:
    """Tier 2: explicit scope without the server returns status=not_found."""
    from reyn.op_runtime.mcp_drop_server import handle as drop_handle

    _seed_config(tmp_path / "reyn.local.yaml", {"other": {}})

    op = MCPDropServerIROp(
        kind="mcp_drop_server", server="missing_server", scope="local",
        clear_secrets=False,
    )
    result = _run(drop_handle(op=op, ctx=_make_op_ctx(tmp_path), caller="control_ir"))

    assert result["status"] == "not_found"
    assert result["server"] == "missing_server"
    assert result["scope"] == "local"


def test_mcp_drop_server_not_found_auto_scope(tmp_path: Path) -> None:
    """Tier 2: auto-detect with no matching scope returns status=not_found."""
    from reyn.op_runtime.mcp_drop_server import handle as drop_handle

    # No config files exist at all
    op = MCPDropServerIROp(
        kind="mcp_drop_server", server="ghost", scope=None,
        clear_secrets=False,
    )
    result = _run(drop_handle(op=op, ctx=_make_op_ctx(tmp_path), caller="control_ir"))

    assert result["status"] == "not_found"
    assert result["server"] == "ghost"
    assert result["scope"] is None


def test_mcp_drop_server_empty_server_raises(tmp_path: Path) -> None:
    """Tier 2: empty server string raises ValueError (= input validation)."""
    from reyn.op_runtime.mcp_drop_server import handle as drop_handle

    op = MCPDropServerIROp(kind="mcp_drop_server", server="   ", scope="local")
    with pytest.raises(ValueError, match="non-empty"):
        _run(drop_handle(op=op, ctx=_make_op_ctx(tmp_path), caller="control_ir"))


# ── 3. P6 event emission ──────────────────────────────────────────────────


def test_mcp_drop_server_emits_p6_event_on_success(tmp_path: Path) -> None:
    """Tier 2: mcp_server_removed event emitted with audit metadata."""
    from reyn.op_runtime.mcp_drop_server import handle as drop_handle

    cfg_path = tmp_path / "reyn.local.yaml"
    _seed_config(cfg_path, {
        "filesystem": {
            "command": "npx",
            "env": {"FS_TOKEN": "${FS_TOKEN}", "FS_URL": "${FS_URL}"},
        },
    })

    events = _CapturingEvents()
    op = MCPDropServerIROp(
        kind="mcp_drop_server", server="filesystem", scope="local",
        clear_secrets=False,
    )
    _run(drop_handle(
        op=op,
        ctx=_make_op_ctx(tmp_path, events=events),
        caller="control_ir",
    ))

    # P6 event check
    names = [name for name, _ in events.events]
    assert "mcp_server_removed" in names
    payload = next(kw for n, kw in events.events if n == "mcp_server_removed")
    assert payload["server"] == "filesystem"
    assert payload["scope"] == "local"
    assert str(cfg_path) == payload["removed_path"]
    assert set(payload["env_keys_captured"]) == {"FS_TOKEN", "FS_URL"}


def test_mcp_drop_server_no_event_on_not_found(tmp_path: Path) -> None:
    """Tier 2: status=not_found does NOT emit mcp_server_removed."""
    from reyn.op_runtime.mcp_drop_server import handle as drop_handle

    events = _CapturingEvents()
    op = MCPDropServerIROp(kind="mcp_drop_server", server="ghost", scope=None)
    _run(drop_handle(
        op=op,
        ctx=_make_op_ctx(tmp_path, events=events),
        caller="control_ir",
    ))

    names = [name for name, _ in events.events]
    assert "mcp_server_removed" not in names


# ── 4. secrets cleanup integration ────────────────────────────────────────


def test_mcp_drop_server_clears_secrets_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: clear_secrets=True removes matching keys from secrets.env."""
    from reyn.op_runtime.mcp_drop_server import handle as drop_handle
    from reyn.secrets.store import list_secret_keys, save_secret

    # Redirect secrets store to tmp_path
    secrets_file = tmp_path / "secrets.env"
    monkeypatch.setenv("REYN_SECRETS_PATH", str(secrets_file))

    # Seed two secrets — one referenced by the server, one not
    save_secret("FS_TOKEN", "value-a")
    save_secret("FS_URL", "value-b")
    save_secret("UNRELATED", "value-c")

    cfg_path = tmp_path / "reyn.local.yaml"
    _seed_config(cfg_path, {
        "filesystem": {
            "command": "npx",
            "env": {"FS_TOKEN": "${FS_TOKEN}", "FS_URL": "${FS_URL}"},
        },
    })

    op = MCPDropServerIROp(
        kind="mcp_drop_server", server="filesystem", scope="local",
        clear_secrets=True,
    )
    result = _run(drop_handle(
        op=op, ctx=_make_op_ctx(tmp_path), caller="control_ir",
    ))

    # The two referenced keys should be gone; UNRELATED stays.
    remaining = set(list_secret_keys())
    assert "FS_TOKEN" not in remaining
    assert "FS_URL" not in remaining
    assert "UNRELATED" in remaining

    # result.env_keys_cleared records what was actually removed
    assert set(result["env_keys_cleared"]) == {"FS_TOKEN", "FS_URL"}


def test_mcp_drop_server_preserves_secrets_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: clear_secrets=False leaves the secrets.env unchanged."""
    from reyn.op_runtime.mcp_drop_server import handle as drop_handle
    from reyn.secrets.store import list_secret_keys, save_secret

    secrets_file = tmp_path / "secrets.env"
    monkeypatch.setenv("REYN_SECRETS_PATH", str(secrets_file))

    save_secret("FS_TOKEN", "value-a")
    save_secret("FS_URL", "value-b")

    cfg_path = tmp_path / "reyn.local.yaml"
    _seed_config(cfg_path, {
        "filesystem": {
            "command": "npx",
            "env": {"FS_TOKEN": "${FS_TOKEN}", "FS_URL": "${FS_URL}"},
        },
    })

    op = MCPDropServerIROp(
        kind="mcp_drop_server", server="filesystem", scope="local",
        clear_secrets=False,
    )
    result = _run(drop_handle(
        op=op, ctx=_make_op_ctx(tmp_path), caller="control_ir",
    ))

    # Secrets file untouched
    remaining = set(list_secret_keys())
    assert "FS_TOKEN" in remaining
    assert "FS_URL" in remaining

    # env_keys_cleared empty
    assert result["env_keys_cleared"] == []


# ── 5. universal_dispatch route ───────────────────────────────────────────


def test_universal_dispatch_routes_drop_server() -> None:
    """Tier 2: mcp.operation__drop_server resolves to mcp_drop_server target."""
    from reyn.tools.universal_dispatch import resolve_invoke_action

    resolved = resolve_invoke_action(
        "mcp.operation__drop_server",
        {"server": "filesystem", "scope": "local"},
    )
    assert resolved.target_tool_name == "mcp_drop_server"
    # Passthrough args — server / scope flow through unchanged
    assert resolved.target_args["server"] == "filesystem"
    assert resolved.target_args["scope"] == "local"


def test_universal_dispatch_describe_drop_server() -> None:
    """Tier 2: describe_action for mcp.operation__drop_server resolves cleanly."""
    from reyn.tools.universal_dispatch import resolve_describe_action

    resolved = resolve_describe_action("mcp.operation__drop_server")
    assert resolved.target_tool_name == "mcp_drop_server"


# ── 6. ToolDefinition registration ────────────────────────────────────────


def test_mcp_drop_server_registered_in_default_registry() -> None:
    """Tier 2: get_default_registry() includes the new mcp_drop_server tool."""
    from reyn.tools import get_default_registry

    registry = get_default_registry()
    td = registry.lookup("mcp_drop_server")
    assert td is not None


def test_mcp_drop_server_is_router_and_phase_visible() -> None:
    """Tier 2: mcp_drop_server is dual-gated (= router + phase, FP-0034 §D23)."""
    from reyn.tools import get_default_registry

    registry = get_default_registry()
    router_names = {t.name for t in registry.for_router()}
    phase_names = {t.name for t in registry.for_phase()}
    assert "mcp_drop_server" in router_names
    assert "mcp_drop_server" in phase_names


def test_mcp_drop_server_tool_schema_requires_server() -> None:
    """Tier 2: ToolDefinition.parameters requires 'server' field."""
    from reyn.tools.mcp_drop import MCP_DROP_SERVER_OP

    required = MCP_DROP_SERVER_OP.parameters.get("required", [])
    assert "server" in required
    # scope and clear_secrets are optional
    assert "scope" not in required
    assert "clear_secrets" not in required


def test_mcp_drop_server_tool_scope_enum_three_tiers() -> None:
    """Tier 2: scope enum is local / project / user (matches mcp_install)."""
    from reyn.tools.mcp_drop import MCP_DROP_SERVER_OP

    scope_prop = MCP_DROP_SERVER_OP.parameters["properties"]["scope"]
    assert set(scope_prop["enum"]) == {"local", "project", "user"}
