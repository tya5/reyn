"""Tier 2: core/registry/source_resolver.py name-normalisation pure helper contracts.

_strip_mcp_prefix(name) strips ecosystem-standard MCP server name prefixes
('mcp-server-', 'server-') from a package name component.

_npm_package_name(identifier) derives a short config key from an npm package
identifier, stripping scope and MCP prefixes.

_pypi_package_name(identifier) derives a short name from a PyPI package
specifier, stripping version constraints and MCP prefixes.
"""
from __future__ import annotations

from reyn.core.registry.source_resolver import (
    _npm_package_name,
    _pypi_package_name,
    _strip_mcp_prefix,
)

# ── _strip_mcp_prefix ─────────────────────────────────────────────────────────


def test_strip_mcp_prefix_strips_server_prefix() -> None:
    """Tier 2: 'server-filesystem' → 'filesystem' (strips 'server-' prefix)."""
    assert _strip_mcp_prefix("server-filesystem") == "filesystem"


def test_strip_mcp_prefix_strips_mcp_server_prefix() -> None:
    """Tier 2: 'mcp-server-foo' → 'foo' (strips 'mcp-server-' prefix)."""
    assert _strip_mcp_prefix("mcp-server-foo") == "foo"


def test_strip_mcp_prefix_no_match_returns_input() -> None:
    """Tier 2: name with no matching prefix is returned unchanged."""
    assert _strip_mcp_prefix("my-plain-name") == "my-plain-name"


def test_strip_mcp_prefix_requires_non_empty_suffix() -> None:
    """Tier 2: prefix-only input is returned unchanged (suffix must be non-empty)."""
    assert _strip_mcp_prefix("server-") == "server-"


# ── _npm_package_name ─────────────────────────────────────────────────────────


def test_npm_package_name_scoped_with_mcp_server_prefix() -> None:
    """Tier 2: '@modelcontextprotocol/server-filesystem' → 'filesystem'."""
    assert _npm_package_name("@modelcontextprotocol/server-filesystem") == "filesystem"


def test_npm_package_name_scoped_plain() -> None:
    """Tier 2: '@scope/plain-name' → 'plain-name' (scope stripped, no MCP prefix)."""
    assert _npm_package_name("@scope/plain-name") == "plain-name"


def test_npm_package_name_unscoped_with_mcp_prefix() -> None:
    """Tier 2: 'mcp-server-foo' → 'foo' (MCP prefix stripped)."""
    assert _npm_package_name("mcp-server-foo") == "foo"


def test_npm_package_name_unscoped_plain() -> None:
    """Tier 2: 'plain-package' → 'plain-package' (no prefix, returned as-is)."""
    assert _npm_package_name("plain-package") == "plain-package"


# ── _pypi_package_name ────────────────────────────────────────────────────────


def test_pypi_package_name_strips_mcp_server_prefix() -> None:
    """Tier 2: 'mcp-server-time' → 'time'."""
    assert _pypi_package_name("mcp-server-time") == "time"


def test_pypi_package_name_strips_version_specifier() -> None:
    """Tier 2: version constraint is stripped before prefix removal."""
    assert _pypi_package_name("mcp-server-time>=1.0") == "time"


def test_pypi_package_name_no_prefix_returns_as_is() -> None:
    """Tier 2: name without MCP prefix is returned unchanged."""
    assert _pypi_package_name("my-mcp-tool") == "my-mcp-tool"


def test_pypi_package_name_underscore_normalised_to_hyphen() -> None:
    """Tier 2: underscores are converted to hyphens before prefix lookup."""
    assert _pypi_package_name("mcp_server_fetch") == "fetch"
