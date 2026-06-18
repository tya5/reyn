"""Tier 2: #311 — reyn.safe / reyn.interfaces.api.{safe,unsafe} relocated to
reyn.api.{safe,unsafe} as a CLEAN BREAK (no backward-compat shims, owner policy).

The safe-mode helpers moved to reyn.api.safe.* and the unsafe helpers to
reyn.api.unsafe.*; the old import paths are DELETED (no shim). This pins: the
canonical reyn.api.* imports work; the old paths no longer import; and the
safe-mode allowlist is allow-of-one (only reyn.api.safe.*; everything else,
incl reyn.api.unsafe.* and the removed reyn.safe.*/reyn.unsafe.*, is
default-deny).
"""
from __future__ import annotations

import importlib

import pytest

from reyn.core.kernel._python_allowlist import module_is_allowed


def test_canonical_api_safe_imports() -> None:
    """Tier 2: reyn.api.safe.* (the canonical path) imports."""
    import reyn.api.safe.file  # noqa: F401
    import reyn.api.safe.mcp.registry  # noqa: F401
    assert importlib.import_module("reyn.api.safe.http") is not None


def test_canonical_api_unsafe_imports() -> None:
    """Tier 2: reyn.api.unsafe.* (the canonical path) imports."""
    import reyn.api.unsafe.shell  # noqa: F401
    assert importlib.import_module("reyn.api.unsafe.workspace") is not None


@pytest.mark.parametrize("old_path", [
    "reyn.safe",
    "reyn.safe.file",
    "reyn.interfaces.api.safe",
    "reyn.interfaces.api.unsafe",
    "reyn.interfaces.api.unsafe.shell",
])
def test_old_paths_are_gone_clean_break(old_path: str) -> None:
    """Tier 2: clean break — the pre-#311 paths no longer import (no shim)."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(old_path)


def test_allowlist_is_allow_of_one() -> None:
    """Tier 2: only reyn.api.safe.* is allowed; everything else — incl
    reyn.api.unsafe.* and the removed reyn.safe.*/reyn.unsafe.* — is default-deny."""
    e: set[str] = set()
    # canonical safe — allowed
    assert module_is_allowed("reyn.api.safe", e)
    assert module_is_allowed("reyn.api.safe.file", e)
    assert module_is_allowed("reyn.api.safe.mcp.registry", e)
    # unsafe (new) — default-deny, no explicit reject needed
    assert not module_is_allowed("reyn.api.unsafe", e)
    assert not module_is_allowed("reyn.api.unsafe.shell", e)
    # removed pre-#311 paths — now default-deny (clean break)
    assert not module_is_allowed("reyn.safe", e)
    assert not module_is_allowed("reyn.safe.file", e)
    assert not module_is_allowed("reyn.unsafe", e)
    # arbitrary non-stdlib reyn module — default-deny
    assert not module_is_allowed("reyn.runtime.session", e)
    # a pure-stdlib module still allowed (fall-through intact)
    assert module_is_allowed("json", e)
