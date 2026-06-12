"""Tier 2: #1503 — mcp_install empty-config guard + list_tools error surface.

Bug 1 (mcp_install.py): GitHub URL for unknown repo (no npm/pypi entry)
produced packages_raw=[], causing server_entry={} to be written to
mcp.yaml as an empty dict while returning status:ok. Fix: fail loud with
status:error when server_entry has neither command nor url.

Bug 2 (tools/mcp.py _handle_list_mcp_tools): error dict in mcp_list_tools
result (e.g. connection refused) was silently dropped by the name-filter
loop, returning {"mcp_tools":[]} with no explanation. Fix: detect "error"
key early and return {"error": ...} so _normalise_router_tool_result passes
it through verbatim to the LLM.

Regression fixture: https://github.com/mrkrsl/web-search-mcp
(unknown runtime — no npm/pypi/docker package list in source_resolver)
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.events.events import EventLog
from reyn.op_runtime.context import OpContext
from reyn.op_runtime.mcp_install import handle as mcp_install_handle
from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.registry.source_resolver import resolve
from reyn.schemas.models import MCPInstallIROp
from reyn.tools.mcp import _handle_list_mcp_tools
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.user_intervention import InterventionAnswer, UserIntervention

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REGRESSION_FIXTURE_URL = "https://github.com/mrkrsl/web-search-mcp"


class _AutoApproveInterventionBus:
    def __init__(self) -> None:
        self.requests: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.requests.append(iv)
        if iv.choices:
            return InterventionAnswer(choice_id="always")
        return InterventionAnswer(text="test-secret-value")


def _make_resolver(tmp_path: Path) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={"mcp_install": "allow"},
        project_root=tmp_path,
        interactive=True,
    )


def _make_source_decl(resolver: PermissionResolver) -> PermissionDecl:
    canonical_config = str(resolver._project_root / ".reyn" / "mcp.yaml")
    resolver.session_approve_path(canonical_config, "mcp_install_test", "file.write")
    return PermissionDecl(
        file_write=[{"path": canonical_config, "scope": "just_path"}],
        secret_write=["*"],
    )


def _make_op_ctx(
    tmp_path: Path,
    resolver: PermissionResolver,
    decl: PermissionDecl,
) -> OpContext:
    events = EventLog()
    workspace = type("Workspace", (), {"root": str(tmp_path)})()
    return OpContext(
        workspace=workspace,
        events=events,
        permission_decl=decl,
        permission_resolver=resolver,
        skill_name="mcp_install_test",
        intervention_bus=_AutoApproveInterventionBus(),
    )


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Part 1: Bug 1 — GitHub unknown-runtime guard
# ---------------------------------------------------------------------------


class TestBug1GitHubUnknownRuntimeGuard:
    def test_regression_fixture_resolver_returns_empty_packages(self):
        """Tier 2: regression fixture URL resolves with empty packages_raw.

        Verifies that https://github.com/mrkrsl/web-search-mcp (an unknown
        repo) produces runtime_hint="" and packages_raw=[] — the precondition
        that previously caused Bug 1.
        """
        r = resolve(_REGRESSION_FIXTURE_URL)
        assert r.error == "", f"unexpected resolve error: {r.error}"
        assert r.runtime_hint == "", "expected unknown runtime_hint for unknown repo"
        assert r.packages_raw == [], "expected empty packages_raw for unknown repo"
        assert r.server_name != "", "server_name should be derived from repo slug"

    def test_github_unknown_runtime_returns_error_not_ok(self, tmp_path):
        """Tier 2: mcp_install with unknown GitHub URL returns status:error.

        Before the fix, the handler wrote {server_name: {}} to mcp.yaml and
        returned status:ok. After the fix, it must return status:error with
        guidance text, and must NOT write an empty server entry.
        """
        resolver = _make_resolver(tmp_path)
        decl = _make_source_decl(resolver)
        ctx = _make_op_ctx(tmp_path, resolver, decl)

        op = MCPInstallIROp(
            kind="mcp_install",
            server_id=_REGRESSION_FIXTURE_URL,
            scope="local",
            source=_REGRESSION_FIXTURE_URL,
        )

        result = _run(mcp_install_handle(op, ctx, "control_ir"))

        assert result["status"] == "error", (
            f"expected status:error for unknown-runtime GitHub URL, got {result['status']}"
        )
        assert "error" in result, "result must carry an 'error' explanation key"
        # Guidance must point the user to explicit prefix options
        error_text = result["error"]
        assert "npm:" in error_text or "pypi:" in error_text or "docker:" in error_text, (
            f"error message must mention explicit prefix alternatives, got: {error_text!r}"
        )

    def test_github_unknown_runtime_does_not_write_config(self, tmp_path):
        """Tier 2: mcp_install error path must not write a broken empty config.

        Regression guard: before the fix, mcp.yaml was written with
        {server_name: {}} even on failure.
        """
        resolver = _make_resolver(tmp_path)
        decl = _make_source_decl(resolver)
        ctx = _make_op_ctx(tmp_path, resolver, decl)

        op = MCPInstallIROp(
            kind="mcp_install",
            server_id=_REGRESSION_FIXTURE_URL,
            scope="local",
            source=_REGRESSION_FIXTURE_URL,
        )

        _run(mcp_install_handle(op, ctx, "control_ir"))

        # mcp.yaml must not exist (nothing was written) OR it must not contain
        # an empty server entry for the web-search server name.
        config_path = tmp_path / ".reyn" / "mcp.yaml"
        if config_path.exists():
            import yaml
            written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            servers = written.get("mcp", {}).get("servers", {})
            for entry in servers.values():
                assert entry, (
                    f"mcp.yaml must not contain empty server entries, got: {servers}"
                )


# ---------------------------------------------------------------------------
# Part 2: Bug 2 — list_tools error surfacing
# ---------------------------------------------------------------------------


class _ErrorHostStub:
    """Real stub host whose mcp_list_tools returns an error dict.

    Simulates the MCP client returning an error entry (e.g. connection
    refused) in the tool listing response.
    """

    def __init__(self, error_message: str) -> None:
        self._error_message = error_message

    async def mcp_list_tools(self, server: str) -> list[dict]:
        return [{"error": self._error_message}]


class _NormalHostStub:
    """Real stub host returning a normal tool list (control path)."""

    async def mcp_list_tools(self, server: str) -> list[dict]:
        return [
            {"name": "search", "description": "Search the web", "inputSchema": {}},
        ]


def _make_router_ctx(host: object) -> ToolContext:
    rs = RouterCallerState(host=host)
    return ToolContext(
        caller_kind="router",
        router_state=rs,
        events=EventLog(),
        permission_resolver=None,
        workspace=None,
    )


class TestBug2ListToolsErrorSurface:
    def test_error_dict_in_result_is_surfaced(self):
        """Tier 2: _handle_list_mcp_tools returns {"error":...} when host errors.

        Before the fix, an error dict in the MCP result (no "name" key) was
        silently dropped and {"mcp_tools":[]} was returned. After the fix,
        {"error": <message>} is returned so the LLM can diagnose the failure.
        """
        host = _ErrorHostStub("connection refused to MCP server")
        ctx = _make_router_ctx(host)
        result = _run(_handle_list_mcp_tools({"server": "web-search"}, ctx))

        assert "error" in result, (
            f"expected 'error' key in result, got: {result}"
        )
        assert "connection refused" in result["error"], (
            f"error message should be forwarded, got: {result['error']!r}"
        )

    def test_error_result_has_no_mcp_tools_key(self):
        """Tier 2: error return must NOT include mcp_tools key.

        _normalise_router_tool_result in router_loop.py unwraps "mcp_tools"
        and drops everything else — returning {"error":...,"mcp_tools":[]}
        would cause the error to be silently lost. The fix returns only
        {"error":...} so the normaliser passes it through verbatim.
        """
        host = _ErrorHostStub("timeout")
        ctx = _make_router_ctx(host)
        result = _run(_handle_list_mcp_tools({"server": "any-server"}, ctx))

        assert "mcp_tools" not in result, (
            "error return must not include mcp_tools key — normaliser would unwrap it"
        )

    def test_normal_result_still_returns_mcp_tools(self):
        """Tier 2: normal (non-error) host result returns {"mcp_tools":[...]} unchanged.

        Regression guard: the error-detection branch must not affect the
        successful path.
        """
        host = _NormalHostStub()
        ctx = _make_router_ctx(host)
        result = _run(_handle_list_mcp_tools({"server": "web-search"}, ctx))

        assert "mcp_tools" in result, f"expected mcp_tools key, got: {result}"
        names = [t["name"] for t in result["mcp_tools"]]
        assert "web-search__search" in names, (
            f"expected server-prefixed tool name in result, got: {names}"
        )
