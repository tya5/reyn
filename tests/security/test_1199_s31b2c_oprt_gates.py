"""Tier 2: op-runtime gate cutovers + model axis additions (#1199 S3.1b-2c).

The clean op-runtime gates (require_secret_write / require_tool)
route their static decl authority through the unified EffectivePermission model,
byte-identical (each gate's existing suite is the broad guard). Adds the
SECRET_WRITE "*" wildcard (require_secret_write). The TOOL axis this file
used to also pin here (``decl.tool``, ``require_tool``) was DELETED by
#5848 (architect ruling) — zero production populators, and its
empty-list-means-deny-all semantics were the OPPOSITE of ContextualLayer's
own reading of the same axis. ``AgentLayer`` no longer constrains TOOL at
all (see ``test_tool_axis_is_unconstrained_by_agent_layer`` below, the
new control for that fact).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.security.permissions.effective import AgentLayer, CapabilityAxis
from reyn.security.permissions.permissions import PermissionDecl
from tests._support.permissions import make_resolver as _make_resolver

AX = CapabilityAxis


def test_tool_axis_is_unconstrained_by_agent_layer() -> None:
    """Tier 2: #5848 — AgentLayer no longer has any TOOL-axis opinion; a bare
    decl (no `tool` field exists any more) allows every name, matching the
    PYTHON(removed)/SKILL(removed) fallthrough this method already has for
    other retired axes. The ONE live TOOL-axis authority is
    ContextualLayer (#5841's dispatch_tool call-time check) — this test's
    own job is only the negative control: AgentLayer alone must never
    re-deny what it used to gate.

    Falsify note (verified in-file, Edit-only, then reverted): reinstating
    the deleted `if axis is CapabilityAxis.TOOL: return value in d.tool`
    branch makes this RED for an undeclared name."""
    assert AgentLayer(PermissionDecl()).allows(AX.TOOL, "grep") is True
    assert AgentLayer(PermissionDecl()).allows(AX.TOOL, "anything_at_all") is True


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
