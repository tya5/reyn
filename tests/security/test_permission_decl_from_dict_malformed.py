"""Tier 2: PermissionDecl.from_dict fails secure on a malformed permissions block.

Found via bug-mining (2026-06-20). A skill.md with `permissions: <string|list>`
(an authoring typo) is not coerced by the parser (`parser.py` uses `or {}`,
which keeps a truthy non-dict) and reaches the compiler via `expander.py:210`
unguarded — `d.get(...)` on a str/list then crashed with an unclear
AttributeError on skill expansion. The skill *validator* guards its own path,
but the *expander* did not.

Fix: `from_dict` defaults a non-dict to an EMPTY decl (no grants) — crash-safe
AND the secure default for a permissions primitive (a malformed block must not
silently grant anything).

Falsification: pre-fix a non-dict raised AttributeError; the valid-decl test
proves the guard doesn't swallow real grants.
"""
from __future__ import annotations

import pytest

from reyn.security.permissions.permissions import PermissionDecl


@pytest.mark.parametrize("bad", ["allow", ["file.write"], 42, ("x",)])
def test_malformed_permissions_yield_empty_decl(bad) -> None:
    """Tier 2: a non-dict permissions value → empty decl (no crash, no grants)."""
    decl = PermissionDecl.from_dict(bad)
    # fail-secure: nothing is granted from a malformed block
    assert not decl.file_write
    assert not decl.mcp
    assert not decl.tool


def test_none_still_yields_empty_decl() -> None:
    """Tier 2: the pre-existing None/falsy resilience is preserved."""
    assert PermissionDecl.from_dict(None) is not None
    assert PermissionDecl.from_dict({}) is not None


def test_valid_permissions_still_parse() -> None:
    """Tier 2: a well-formed permissions block is unaffected (regression guard).

    Falsification: if the guard were too broad, real grants would be dropped.
    """
    decl = PermissionDecl.from_dict(
        {"file.write": [{"path": "out.txt", "scope": "just_path"}]}
    )
    assert decl.file_write, "a valid file.write grant must survive"
