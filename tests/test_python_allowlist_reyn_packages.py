"""Tier 2: safe-mode allowlist accepts reyn.safe.* and rejects reyn.unsafe.*.

OS invariant from FP-0014 / ADR-G Phase 1:

  - Safe-mode python steps may import from the Reyn-vetted `reyn.safe`
    package and its submodules (= helpers callable from sandboxed code).
  - Safe-mode python steps must NOT import from `reyn.unsafe.*` (=
    helpers reserved for unsafe-mode steps, explicit defence even though
    `reyn` is not otherwise in the stdlib allowlist).

Verified via the public `module_is_allowed` helper (= same predicate the
harness AST validator uses).
"""
from __future__ import annotations

from reyn.kernel._python_allowlist import module_is_allowed


def test_reyn_safe_root_is_allowed():
    """Tier 2: bare `reyn.safe` import allowed."""
    assert module_is_allowed("reyn.safe", frozenset()) is True


def test_reyn_safe_submodule_is_allowed():
    """Tier 2: `reyn.safe.hash` (or any submodule) is allowed."""
    assert module_is_allowed("reyn.safe.hash", frozenset()) is True
    assert module_is_allowed("reyn.safe.json", frozenset()) is True


def test_reyn_unsafe_root_is_rejected():
    """Tier 2: bare `reyn.unsafe` import rejected from safe mode."""
    assert module_is_allowed("reyn.unsafe", frozenset()) is False


def test_reyn_unsafe_submodule_is_rejected():
    """Tier 2: `reyn.unsafe.file` (or any submodule) rejected from safe mode."""
    assert module_is_allowed("reyn.unsafe.file", frozenset()) is False
    assert module_is_allowed("reyn.unsafe.http", frozenset()) is False


def test_reyn_bare_remains_rejected():
    """Tier 2: bare `reyn` import (not `reyn.safe.*`) still rejected.

    Only `reyn.safe.*` is allowlisted — `reyn` itself or any other
    `reyn.X` is rejected (the implicit "top-level not in PURE_STDLIB_ALLOWLIST"
    path).
    """
    assert module_is_allowed("reyn", frozenset()) is False
    assert module_is_allowed("reyn.kernel", frozenset()) is False
    assert module_is_allowed("reyn.events", frozenset()) is False


def test_stdlib_still_works():
    """Tier 2: regression guard — stdlib allowlist unchanged."""
    assert module_is_allowed("json", frozenset()) is True
    assert module_is_allowed("math", frozenset()) is True
    assert module_is_allowed("os", frozenset()) is False
    assert module_is_allowed("subprocess", frozenset()) is False
