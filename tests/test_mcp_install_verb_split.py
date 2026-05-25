"""Tier 2: 2026-05-25 mcp install 3-verb split — schema, dispatch, composition.

The previous single ``mcp__install_server`` accepted ``server_id`` XOR
``source`` with a ``required=[]`` schema, leaving the LLM to pick between
two parallel paths from a flat string surface. This was split into three
verbs along the **source axis**:

  - ``mcp__install_registry`` — official MCP registry (= ``server_id``)
  - ``mcp__install_package``  — structured {kind, identifier, version?}
                                for npm / pypi / docker / github
  - ``mcp__install_local``    — direct ``{name, command, args}`` write
                                of an ``.reyn/mcp.yaml`` stdio entry

This file pins the contract:

  - Each verb's required-fields set (= structural validation gives the
    schema teeth; LLM can't reach a "both empty" XOR error state).
  - Each verb's routing target in ``_OPERATION_RULES``.
  - ``_build_source_string`` composes the source_resolver inline string
    correctly for all 4 kinds (npm/pypi/docker/github) with and without
    version.
  - ``mcp__install_local`` writes a structurally valid entry to a fresh
    ``.reyn/mcp.yaml`` (= MCPClient-loadable stdio shape).

No mocks. Real ToolDefinition + real source-string composer + real yaml
file writes against tmp_path.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from reyn.tools.mcp_verbs import (
    MCP_INSTALL_LOCAL,
    MCP_INSTALL_PACKAGE,
    MCP_INSTALL_REGISTRY,
    MCP_SEARCH_REGISTRY,
    _build_source_string,
)
from reyn.tools.types import ToolContext
from reyn.tools.universal_dispatch import resolve_invoke_action


class _FakeEvents:
    def emit(self, *args: Any, **kwargs: Any) -> None:
        pass


def _ctx() -> ToolContext:
    return ToolContext(
        events=_FakeEvents(),
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=None,
    )


# ── A. Required-fields contract ────────────────────────────────────────────────


def test_install_registry_required_is_server_id_only() -> None:
    """Tier 1: install_registry requires server_id; no source escape hatch."""
    schema = MCP_INSTALL_REGISTRY.parameters
    assert schema["required"] == ["server_id"]
    props = schema["properties"]
    assert "server_id" in props
    # Source field must NOT leak back — the whole point of the split.
    assert "source" not in props
    assert "scope" not in props


def test_install_package_required_is_kind_and_identifier() -> None:
    """Tier 1: install_package requires (kind, identifier); version optional."""
    schema = MCP_INSTALL_PACKAGE.parameters
    assert set(schema["required"]) == {"kind", "identifier"}
    props = schema["properties"]
    assert set(props["kind"]["enum"]) == {"npm", "pypi", "docker", "github"}
    assert "identifier" in props
    assert "version" in props


def test_install_local_required_is_name_command_args() -> None:
    """Tier 1: install_local requires (name, command, args); env_overrides optional."""
    schema = MCP_INSTALL_LOCAL.parameters
    assert set(schema["required"]) == {"name", "command", "args"}
    props = schema["properties"]
    assert props["args"]["type"] == "array"
    assert props["args"]["items"]["type"] == "string"


def test_search_registry_required_is_text() -> None:
    """Tier 1: search_registry requires text (renamed from search_server)."""
    schema = MCP_SEARCH_REGISTRY.parameters
    assert schema["required"] == ["text"]


# ── B. Universal-dispatch routing ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "qn, target",
    [
        ("mcp__search_registry",  "mcp_search_registry"),
        ("mcp__install_registry", "mcp_install_registry"),
        ("mcp__install_package",  "mcp_install_package"),
        ("mcp__install_local",    "mcp_install_local"),
    ],
)
def test_verb_routes_to_handler(qn: str, target: str) -> None:
    """Tier 2: each new verb resolves to its dedicated handler in the registry."""
    resolved = resolve_invoke_action(qn, {})
    assert resolved.target_tool_name == target


# ── C. _build_source_string composition (npm/pypi/docker/github) ──────────────


@pytest.mark.parametrize(
    "kind, identifier, version, expected",
    [
        ("npm",    "@scope/server-foo",  "1.0.0",  "npm:@scope/server-foo@1.0.0"),
        ("npm",    "plain-pkg",          "",       "npm:plain-pkg"),
        ("pypi",   "my-mcp-server",      "0.6.2",  "pypi:my-mcp-server==0.6.2"),
        ("pypi",   "my-mcp-server",      "",       "pypi:my-mcp-server"),
        ("docker", "org/img",            "v1",     "docker:org/img:v1"),
        ("docker", "org/img",            "",       "docker:org/img"),
        ("github", "https://github.com/owner/repo", "ignored",
            "https://github.com/owner/repo"),
    ],
)
def test_build_source_string_composes_correctly(
    kind: str, identifier: str, version: str, expected: str,
) -> None:
    """Tier 2: structured (kind, identifier, version?) → source_resolver inline form.

    The LLM passes structured fields; the handler composes the inline
    string the existing source_resolver consumes. github is the outlier
    (URL is the specifier itself; version is ignored).
    """
    assert _build_source_string(kind, identifier, version) == expected


# ── D. install_local writes a loader-compatible stdio entry ────────────────────


def test_install_local_writes_stdio_entry(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: install_local persists {type: stdio, command, args} to .reyn/mcp.yaml.

    The written entry shape must match the MCPClient loader's stdio
    config contract (= ``type``, ``command``, ``args`` keys present and
    well-typed) so the spawned process is launchable without further
    metadata.
    """
    monkeypatch.chdir(tmp_path)

    result = asyncio.run(MCP_INSTALL_LOCAL.handler(
        {
            "name": "weather",
            "command": "python",
            "args": ["/tmp/weather_mcp.py", "--port", "8080"],
            "env_overrides": {"API_KEY": "fake"},
        },
        _ctx(),
    ))
    assert result["status"] == "ok"
    config_path = Path(result["data"]["config_path"])
    assert config_path.exists()

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    entry = data["mcp"]["servers"]["weather"]
    assert entry["type"] == "stdio"
    assert entry["command"] == "python"
    assert entry["args"] == ["/tmp/weather_mcp.py", "--port", "8080"]
    assert entry["env"] == {"API_KEY": "fake"}


def test_install_local_rejects_missing_command(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: install_local returns an error envelope when command is empty."""
    monkeypatch.chdir(tmp_path)

    result = asyncio.run(MCP_INSTALL_LOCAL.handler(
        {"name": "x", "command": "", "args": []}, _ctx(),
    ))
    assert result["status"] == "error"
    assert "command" in result["data"]["error"]


def test_install_local_rejects_non_list_args(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: install_local returns an error envelope when args is not a list."""
    monkeypatch.chdir(tmp_path)

    result = asyncio.run(MCP_INSTALL_LOCAL.handler(
        {"name": "x", "command": "python", "args": "not-a-list"}, _ctx(),
    ))
    assert result["status"] == "error"
    assert "args" in result["data"]["error"]


# ── E. install_registry rejects empty server_id (no source fallback) ──────────


def test_install_registry_rejects_empty_server_id() -> None:
    """Tier 2: install_registry without server_id is an immediate error.

    Pre-split the handler accepted ``server_id="" AND source=<inline>`` as
    a valid input; this test pins that the source escape hatch is gone
    and that calling with no server_id surfaces a clear error pointing
    the LLM at the other two verbs.
    """
    result = asyncio.run(MCP_INSTALL_REGISTRY.handler({}, _ctx()))
    assert result["status"] == "error"
    msg = result["data"]["error"]
    assert "server_id is required" in msg
    # Cross-reference the two alternative verbs so the LLM can recover.
    assert "mcp__install_package" in msg
    assert "mcp__install_local" in msg


# ── F. install_package rejects invalid kind ───────────────────────────────────


def test_install_package_rejects_unknown_kind() -> None:
    """Tier 2: install_package validates kind against the enum at handler entry."""
    result = asyncio.run(MCP_INSTALL_PACKAGE.handler(
        {"kind": "cargo", "identifier": "foo"}, _ctx(),
    ))
    assert result["status"] == "error"
    assert "kind must be one of" in result["data"]["error"]
