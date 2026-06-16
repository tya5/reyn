"""Tier 2: OS invariant — the benchmark permission pre-approval documented
in reyn.local.yaml.example is a SHAPE the real PermissionResolver accepts.

PR-N9 wired the benchmark Agent with ``intervention_bus=None`` to escape
the tty-prompt hang. Without a bus, the permission system can't ask, so a
skill that declares a write surface (e.g. swe_bench's ``file.write``)
raises unless the kind is pre-approved in config. PR-N11 documented a
pre-approval snippet in ``reyn.local.yaml.example`` — but the documented
shape was WRONG: it used a nested glob (``file.write: {"*": allow}``)
which ``PermissionResolver._is_config_approved`` never matches, so a
benchmark using that snippet would still fail closed.

PR-N12 corrects the doc to the flat ``file.write: allow`` shape and
replaces the prior YAML-round-trip test (= false-confidence format pin
that never invoked the loader) with a REAL functional invariant: build a
``PermissionResolver`` from the documented config and assert
``require_file_write`` actually clears for an out-of-zone path. The
contract under test is "paste the documented snippet → benchmark writes
are approved", end to end through the real permission mechanism.

No mocks. Real ``PermissionResolver`` + real ``require_file_write`` +
real tmp_path. Per testing policy: Fake/real over Mock.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver

_EXAMPLE_PATH = Path(__file__).resolve().parents[1] / "reyn.local.yaml.example"


def _out_of_zone_path(tmp_path: Path) -> str:
    """An absolute path outside the default write zone (.reyn/ , reyn/).

    A plain tmp file under a non-reyn directory is out of zone, so
    require_file_write must consult config / approvals rather than
    auto-granting via the default-zone shortcut.
    """
    return str(tmp_path / "workspace" / "patch_target.py")


@pytest.mark.asyncio
async def test_flat_config_shape_clears_require_file_write(tmp_path: Path) -> None:
    """Tier 2: a PermissionResolver built from the DOCUMENTED flat shape
    (``file.write: allow``) clears ``require_file_write`` for an
    out-of-zone path without raising.

    This is the functional contract PR-N11 meant to pin but didn't: the
    documented snippet, fed to the real resolver, actually approves the
    benchmark's write surface.
    """
    resolver = PermissionResolver(
        {"file.write": "allow"},
        project_root=tmp_path,
        interactive=False,
    )
    decl = PermissionDecl()  # empty — no per-path declaration
    # Must NOT raise: config grant covers the write-class kind.
    await resolver.require_file_write(decl, _out_of_zone_path(tmp_path), "swe_bench")


@pytest.mark.asyncio
async def test_nested_glob_shape_does_not_clear_require_file_write(tmp_path: Path) -> None:
    """Tier 2: the WRONG nested-glob shape (``file.write: {"*": allow}``)
    does NOT clear ``require_file_write`` — it raises PermissionError.

    This pins the regression PR-N12 fixes: the PR-N11 documented snippet
    (a nested glob dict) is silently unmatched by ``_is_config_approved``,
    so a benchmark using it would still fail closed. The doc must use the
    flat shape; this test fails if anyone reintroduces the glob form as
    "supported".
    """
    resolver = PermissionResolver(
        {"file.write": {"*": "allow"}},
        project_root=tmp_path,
        interactive=False,
    )
    decl = PermissionDecl()
    with pytest.raises(PermissionError):
        await resolver.require_file_write(decl, _out_of_zone_path(tmp_path), "swe_bench")


@pytest.mark.asyncio
async def test_nested_to_string_shape_also_clears(tmp_path: Path) -> None:
    """Tier 2: the alternate nested-to-string shape (``file: {write: allow}``)
    also clears — documenting that the loader accepts both the flat
    dotted-key and the nested-to-string forms (but NOT the glob dict).
    """
    resolver = PermissionResolver(
        {"file": {"write": "allow"}},
        project_root=tmp_path,
        interactive=False,
    )
    decl = PermissionDecl()
    await resolver.require_file_write(decl, _out_of_zone_path(tmp_path), "swe_bench")


def test_example_documents_flat_file_write_allow() -> None:
    """Tier 2: reyn.local.yaml.example documents the FLAT shape, not the glob.

    Source-text guard: the example must show ``file.write: allow`` and
    must NOT show the nested-glob form that fails closed. Catches a
    regression back to the PR-N11 mistake.
    """
    src = _EXAMPLE_PATH.read_text(encoding="utf-8")
    assert "file.write: allow" in src, (
        "reyn.local.yaml.example must document the flat 'file.write: allow' "
        "shape — the only form the config permission layer accepts for "
        "benchmark pre-approval."
    )
    # The broken nested-glob form must not be presented as a paste-able
    # snippet line. The WHY prose may mention it inline (= explaining why
    # it fails), so we only reject the standalone paste form: a comment
    # line whose content, after stripping the leading '#' and whitespace,
    # *starts with* the glob mapping key.
    for line in src.splitlines():
        stripped = line.lstrip("#").strip()
        assert not stripped.startswith('"*": allow'), (
            f"reyn.local.yaml.example shows the nested-glob paste form on a "
            f"standalone line ({line!r}); _is_config_approved never matches "
            f"it (= the PR-N11 bug). Use the flat 'file.write: allow' form."
        )
