"""Tier 2: op-runtime gate cutovers + model axis additions (#1199 S3.1b-2c).

The clean op-runtime gates (require_shell / require_secret_write / require_tool)
route their static decl authority through the unified EffectivePermission model,
byte-identical (each gate's existing suite is the broad guard). Adds the TOOL axis
(decl.tool, require_tool) and the SECRET_WRITE "*" wildcard (require_secret_write).
The intricate gates (require_http_get / require_python) are deferred — see the
S3.1b-2c PR / a 2c-2 design call (config-deny tiers / prompt flows / perm-return
make the model a marginal, awkward fit).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.permissions.effective import AgentLayer, CapabilityAxis
from reyn.permissions.permissions import PermissionDecl
from tests.test_permissions import _make_resolver

AX = CapabilityAxis


def test_tool_axis_membership() -> None:
    """Tier 2: the new TOOL axis = value in decl.tool (faithful to require_tool)."""
    assert AgentLayer(PermissionDecl(tool=["grep"])).allows(AX.TOOL, "grep") is True
    assert AgentLayer(PermissionDecl(tool=["grep"])).allows(AX.TOOL, "rm") is False


def test_secret_write_wildcard_and_specific() -> None:
    """Tier 2: SECRET_WRITE honors a specific key AND the "*" wildcard (faithful to
    require_secret_write's two declaration shapes)."""
    assert AgentLayer(PermissionDecl(secret_write=["GH_TOKEN"])).allows(AX.SECRET_WRITE, "GH_TOKEN")
    assert not AgentLayer(PermissionDecl(secret_write=["GH_TOKEN"])).allows(AX.SECRET_WRITE, "OTHER")
    assert AgentLayer(PermissionDecl(secret_write=["*"])).allows(AX.SECRET_WRITE, "ANYTHING")  # wildcard


def test_require_secret_write_cutover_reproduces_logic(tmp_path: Path) -> None:
    """Tier 2: require_secret_write (sync, no _approve) routed through the model —
    specific key OR "*" → ok; undeclared → raise. Byte-identical."""
    r = _make_resolver(tmp_path)
    r.require_secret_write(PermissionDecl(secret_write=["K"]), "K")        # declared → ok
    r.require_secret_write(PermissionDecl(secret_write=["*"]), "ANY_KEY")  # wildcard → ok
    with pytest.raises(PermissionError, match="not declared"):
        r.require_secret_write(PermissionDecl(), "K")                      # undeclared → raise


def test_require_shell_gate_denies_when_undeclared(tmp_path: Path) -> None:
    """Tier 2: require_shell's model gate (SUBPROCESS = decl.shell) denies before
    the _approve prompt when shell is not declared. (The shell=True + _approve flow
    is covered by the broad permission suite.)"""
    import asyncio

    r = _make_resolver(tmp_path)
    with pytest.raises(PermissionError, match="not declared"):
        asyncio.run(r.require_shell(PermissionDecl(shell=False), "ls", None))
