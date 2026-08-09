"""Tier 2: #3552 — ``probe_mcp_server`` gates its live connection through
``PermissionResolver.require_mcp`` BEFORE reaching the network, instead of
reaching a model/plugin/registry-supplied server name first and only gating
the config file write that commits afterward.

Pre-#3552, ``mcp_install``'s pre-commit probe (spawn/connect + ``list_tools``)
ran unconditionally: the handler's own permission gate covered only
``file.write`` (the config it mutates) and ``http.get`` (the registry host) —
neither is the MCP axis, and the axis that DOES exist (``require_mcp``,
the same gate a live tool call to an already-installed server goes through)
was never consulted for the probe. So an actor could reach an arbitrary
MCP server (spawn a subprocess / open a socket) purely by naming it in an
install call, before the operator's "Allow access to MCP server X?" decision
and before config becomes authoritative.

The architect classified this as a DIFFERENT class from #3546 (narrowing
inheritance): "network reach before config becomes authoritative" — an
ordering defect, not a missing-inheritance defect. The fix threads
``permission_resolver``/``bus``/``contextual`` into ``probe_mcp_server``
itself (a structural single point all 3 production call sites share:
``mcp_install.handle``, ``mcp_verbs._handle_mcp_install_local``, and
``plugin_install._register_mcp``), and calls ``require_mcp`` FIRST.

Witness (real observable, not absence-by-inspection): the "server" is a
real subprocess whose FIRST action — before it does anything MCP-shaped —
is to create a marker file. A live connection attempt to a stdio server
necessarily spawns that subprocess, so "was the marker file created" is a
direct, unfakeable observation of whether the network/process reach was
ever attempted, independent of whether the (deliberately non-MCP) command
ever completes a real MCP handshake afterward.

No mocks: a real ``PermissionResolver`` (real ``.reyn/approvals.yaml``
I/O), a real subprocess, real ``PermissionDecl``/``MCPGateway`` — the SDK's
transport is exercised for real in the ALLOW case.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from reyn.core.op_runtime.mcp_install import probe_mcp_server
from reyn.security.permissions.permissions import PermissionResolver


def _marker_entry(marker: Path) -> dict:
    """A stdio server whose first (and only) action is to touch ``marker``,
    then exit — real process, no MCP handshake. Any attempt to CONNECT
    (spawn) it leaves the marker on disk; a gate that fires BEFORE the
    connect attempt never spawns it at all."""
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-c", f"import pathlib; pathlib.Path({str(marker)!r}).write_text('reached')"],
        # The sandbox around every MCP subprocess (#2976) denies a write outside
        # the server's own working directory by default — grant the marker's
        # directory explicitly so a REACHED connection can prove itself by
        # writing, without that being confused with the require_mcp gate itself.
        "write_paths": [str(marker.parent)],
    }


def _resolver(tmp_path: Path, *, interactive: bool, config_permissions: dict | None = None) -> PermissionResolver:
    return PermissionResolver(
        config_permissions=config_permissions or {}, project_root=tmp_path,
        interactive=interactive,
    )


@pytest.mark.asyncio
async def test_denied_mcp_gate_reaches_no_connection(tmp_path: Path) -> None:
    """Tier 2: a non-interactive resolver with no approval for the server DENIES
    require_mcp — and the subprocess is never spawned (marker absent). This is
    the #3552 reachability witness: the deny happens BEFORE the network/process
    reach, not merely alongside it."""
    marker = tmp_path / "reached.marker"
    resolver = _resolver(tmp_path, interactive=False)

    with pytest.raises(PermissionError, match="mystery-server"):
        await probe_mcp_server(
            "mystery-server", _marker_entry(marker),
            permission_resolver=resolver, bus=None,
        )

    assert not marker.exists(), (
        "a denied require_mcp gate must prevent ANY subprocess spawn / network "
        "reach — the marker file proves whether the server process ever ran"
    )


@pytest.mark.asyncio
async def test_config_approved_mcp_gate_allows_the_connection(tmp_path: Path) -> None:
    """Tier 2: with the server config-approved (``permissions.mcp.<name>: allow``,
    the SAME mechanism that already gates a live tool call to an installed
    server), require_mcp passes and the probe proceeds to actually spawn the
    server — proving the gate does not just universally deny, and that a
    legitimately-approved probe still reaches the network exactly as before."""
    marker = tmp_path / "reached.marker"
    resolver = _resolver(
        tmp_path, interactive=False,
        config_permissions={"mcp": {"trusted-server": "allow"}},
    )

    # The command is not a real MCP server, so list_tools eventually fails —
    # but only AFTER the gate passed and the process was spawned.
    err = await probe_mcp_server(
        "trusted-server", _marker_entry(marker),
        permission_resolver=resolver, bus=None,
    )
    assert err is not None, "the marker command is not a real MCP server — expected a probe error"
    assert marker.exists(), (
        "an APPROVED require_mcp gate must let the probe reach the server — "
        "the marker file proves the subprocess actually ran"
    )


@pytest.mark.asyncio
async def test_no_resolver_preserves_pre_3552_behavior(tmp_path: Path) -> None:
    """Tier 2: ``permission_resolver=None`` (a caller that never had one — the
    pre-#3552 default) is unaffected: the probe still reaches the server
    directly, byte-identical to before this fix. Guards against the gate
    becoming mandatory and silently breaking a resolver-less caller."""
    marker = tmp_path / "reached.marker"
    err = await probe_mcp_server("anything", _marker_entry(marker))
    assert err is not None  # not a real MCP server
    assert marker.exists(), "permission_resolver=None must not gate the probe at all"


@pytest.mark.asyncio
async def test_contextual_narrowing_denies_even_when_config_approved(tmp_path: Path) -> None:
    """Tier 2: a per-session ``ContextualLayer`` narrowing that denies this
    server wins even when the server is config-approved — the same
    ContextualLayer ∩ that already governs a live tool call to an installed
    server (#2074 S4a) also governs the probe now, not just the file-write."""
    from reyn.security.permissions.effective import ContextualPermission

    marker = tmp_path / "reached.marker"
    resolver = _resolver(
        tmp_path, interactive=False,
        config_permissions={"mcp": {"narrowed-server": "allow"}},
    )
    contextual = ContextualPermission(mcp_deny=frozenset({"narrowed-server"}))

    with pytest.raises(PermissionError, match="narrowed-server"):
        await probe_mcp_server(
            "narrowed-server", _marker_entry(marker),
            permission_resolver=resolver, bus=None, contextual=contextual,
        )
    assert not marker.exists(), "a contextually-denied server must never be reached, config-approval notwithstanding"
