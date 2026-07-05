"""Tier 2: the unsafe python step is removed — python steps are always sandboxed.

Two invariants pin the removal:

1. **Fail-closed load rejection.** A skill / pipeline that still declares
   ``mode: unsafe`` under ``permissions.python`` is rejected at load with a
   clear, actionable error — NEVER silently downgraded to safe (a silent
   downgrade would run code the author believed was unsandboxed). ``mode:
   safe`` (or no mode) continues to parse. The load-time home is
   :meth:`PermissionDecl.from_dict` (where skill.md / reyn.yaml permission
   frontmatter is parsed).

2. **Escape hatch is gone (falsify).** ``import subprocess`` is exactly the
   kind of import the former ``mode: unsafe`` path allowed by skipping AST
   validation. Driven through the REAL CodeActRunner — the only live python
   execution path — it is now blocked regardless: the harness validates the
   safe-mode AST unconditionally (the ``mode``-branching exec function that
   used to bypass it was deleted). Real subprocess + real AF_UNIX socketpair,
   no mocks.
"""
from __future__ import annotations

import pytest

from reyn.core.kernel.codeact_runner import CodeActRunner
from reyn.security.permissions.permissions import PermissionDecl


def test_mode_unsafe_declaration_is_rejected_at_load() -> None:
    """Tier 2: a permissions.python entry with mode: unsafe raises a clear
    ValueError at parse time — fail-closed, never silently downgraded."""
    with pytest.raises(ValueError) as excinfo:
        PermissionDecl.from_dict(
            {"python": [{"function": "extract", "module": "docs", "mode": "unsafe"}]}
        )
    msg = str(excinfo.value)
    # Actionable: names the removed feature and the offending function.
    assert "mode: unsafe" in msg
    assert "removed" in msg
    assert "extract" in msg


def test_mode_safe_declaration_still_parses() -> None:
    """Tier 2: mode: safe (the only surviving mode) parses without error and
    grants no runtime python authority (the axis carries none)."""
    decl = PermissionDecl.from_dict(
        {"python": [{"function": "compute", "module": "stats", "mode": "safe"}]}
    )
    assert isinstance(decl, PermissionDecl)


def test_python_entry_without_mode_still_parses() -> None:
    """Tier 2: a python entry with no mode key parses (safe is the default and
    only behaviour) — the fail-closed check only fires on an explicit unsafe."""
    decl = PermissionDecl.from_dict(
        {"python": [{"function": "compute", "module": "stats"}]}
    )
    assert isinstance(decl, PermissionDecl)


@pytest.mark.asyncio
async def test_import_subprocess_is_blocked_regardless() -> None:
    """Tier 2: falsify — ``import subprocess``, an import the removed unsafe
    mode would have allowed, is blocked in the live execution path regardless.

    Proves the escape hatch is truly gone: there is no python execution route
    that skips the safe-mode AST validator. Were an unsafe/unvalidated exec
    path still present, this snippet would run and ``ok`` would be True."""

    async def dispatch(name: str, args: dict) -> dict:
        return {"status": "ok", "data": None}

    runner = CodeActRunner()
    out = await runner.run(
        code="import subprocess\nresult = subprocess.run(['echo', 'hi'])",
        dispatch=dispatch,
        allow_unsandboxed=True,  # test-only escape to exercise the exec path w/o an OS sandbox
    )
    assert out["ok"] is False  # safe-mode AST rejects the import before exec
    assert "subprocess" in str(out)  # the violation names the blocked import
