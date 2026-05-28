"""Tier 2: FP-0008 PR-H -- PermissionResolver consults decl.file_* in non-interactive mode.

Defect surfaced by sandbox_2 v6 calibration retry (2026-05-28): 3 of 7
aborts hit `file.write was not approved` PermissionError even though
the swe_bench skill declares `file.write: [{path: "*", scope: "recursive"}]`.

Root cause (= class N=2 of PR #1004 Tool-OpContext bridge):
``PermissionResolver.require_file_write`` (and ``_read``) accept a
``decl: PermissionDecl`` parameter but never consult it. The runtime
checks only (a) default zone, (b) config-level allow, (c) saved/session
approval populated by interactive startup prompts. In non-interactive
mode (= benchmark subprocess, ``sys.stdin.isatty() == False``), the
startup prompts cannot fire -> no session approval -> runtime denies
even though the skill DID declare the path.

Fix (= same root-fix-strict pattern as PR #1004): consult
``decl.file_*`` directly in non-interactive mode. Interactive mode is
unchanged so user-decline tracking via ``_session`` is respected.

This file pins:
  1. In non-interactive mode, a declared file.write path is honored.
  2. In non-interactive mode, a declared file.read path is honored.
  3. Wildcard ``"*"`` declared path covers any runtime path.
  4. ``scope: "recursive"`` covers descendants.
  5. ``scope: "just_path"`` (default) covers exact path only.
  6. In INTERACTIVE mode, the new check does NOT fire (= existing
     model preserved, user-decline still respected).
  7. The ``decl`` parameter is documented + used; previous
     un-consulted state would re-introduce the bug.

Tier rule discipline: every test docstring opens with Tier 2; no
mocks; no private-state assertions; no format-pinning.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.permissions.permissions import (
    PermissionDecl,
    PermissionResolver,
    _decl_covers_path,
)

# Section 1: _decl_covers_path helper --------------------------------------


def test_decl_covers_path_wildcard_matches_anything() -> None:
    """Tier 2: declared path '*' covers any runtime path."""
    entries = [{"path": "*", "scope": "recursive"}]
    assert _decl_covers_path(entries, "/tmp/anything/at/all.txt") is True
    assert _decl_covers_path(entries, "/etc/passwd") is True


def test_decl_covers_path_recursive_matches_descendants(tmp_path: Path) -> None:
    """Tier 2: declared scope=recursive covers descendant runtime paths."""
    declared = str(tmp_path / "work")
    (tmp_path / "work").mkdir()
    nested = tmp_path / "work" / "sub" / "file.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("x")
    entries = [{"path": declared, "scope": "recursive"}]
    assert _decl_covers_path(entries, str(nested)) is True
    # The declared path itself also matches
    assert _decl_covers_path(entries, declared) is True


def test_decl_covers_path_just_path_exact_match_only(tmp_path: Path) -> None:
    """Tier 2: declared scope=just_path covers ONLY the exact path."""
    file_a = tmp_path / "a.txt"
    file_a.write_text("a")
    file_b = tmp_path / "b.txt"
    file_b.write_text("b")
    entries = [{"path": str(file_a), "scope": "just_path"}]
    assert _decl_covers_path(entries, str(file_a)) is True
    assert _decl_covers_path(entries, str(file_b)) is False


def test_decl_covers_path_just_path_no_descendant_match(tmp_path: Path) -> None:
    """Tier 2: declared scope=just_path does NOT match descendants."""
    declared_dir = tmp_path / "work"
    declared_dir.mkdir()
    nested = declared_dir / "sub" / "file.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("x")
    entries = [{"path": str(declared_dir), "scope": "just_path"}]
    assert _decl_covers_path(entries, str(nested)) is False


def test_decl_covers_path_empty_entries_returns_false() -> None:
    """Tier 2: empty entries list returns False (= no declared coverage)."""
    assert _decl_covers_path([], "/anything") is False


def test_decl_covers_path_missing_path_field_skipped() -> None:
    """Tier 2: entry without a path field is skipped (= no crash, just no match)."""
    entries = [{"scope": "recursive"}, {"path": "", "scope": "recursive"}]
    assert _decl_covers_path(entries, "/anything") is False


# Section 2: require_file_write integration -------------------------------


def test_require_file_write_honors_declared_wildcard_in_non_interactive(
    tmp_path: Path,
) -> None:
    """Tier 2: non-interactive mode + declared wildcard path -> require_file_write passes.

    The exact swe_bench scenario: skill declares ``file.write:
    [{path: "*", scope: "recursive"}]`` and runs in non-interactive
    mode (= benchmark subprocess). Pre-PR-H this raised
    PermissionError; post-PR-H it passes silently.
    """
    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=False,
    )
    decl = PermissionDecl(
        file_write=[{"path": "*", "scope": "recursive"}],
    )
    out_of_zone = tmp_path.parent / "elsewhere" / "out.txt"
    # Must NOT raise
    resolver.require_file_write(decl, str(out_of_zone), "swe_bench")


def test_require_file_read_honors_declared_wildcard_in_non_interactive(
    tmp_path: Path,
) -> None:
    """Tier 2: non-interactive mode + declared wildcard path -> require_file_read passes."""
    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=False,
    )
    decl = PermissionDecl(
        file_read=[{"path": "*", "scope": "recursive"}],
    )
    out_of_zone = tmp_path.parent / "elsewhere" / "in.txt"
    resolver.require_file_read(decl, str(out_of_zone), "swe_bench")


def test_require_file_write_honors_declared_specific_path_in_non_interactive(
    tmp_path: Path,
) -> None:
    """Tier 2: non-interactive mode + declared specific path -> require_file_write passes."""
    out_of_zone = tmp_path.parent / "elsewhere" / "specific.txt"
    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=False,
    )
    decl = PermissionDecl(
        file_write=[{"path": str(out_of_zone), "scope": "just_path"}],
    )
    resolver.require_file_write(decl, str(out_of_zone), "my_skill")


def test_require_file_write_undeclared_path_still_denied_in_non_interactive(
    tmp_path: Path,
) -> None:
    """Tier 2: non-interactive mode + path NOT in decl -> PermissionError raised.

    The fix is targeted: declared paths are honored, undeclared paths
    are still gated. PR-H does NOT bypass security entirely.
    """
    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=False,
    )
    decl = PermissionDecl(
        file_write=[{"path": str(tmp_path / "declared"), "scope": "just_path"}],
    )
    not_declared = tmp_path.parent / "elsewhere" / "undeclared.txt"
    with pytest.raises(PermissionError):
        resolver.require_file_write(decl, str(not_declared), "my_skill")


# Section 3: interactive mode preservation --------------------------------


def test_require_file_write_interactive_mode_ignores_declaration(
    tmp_path: Path,
) -> None:
    """Tier 2: interactive mode does NOT consult decl (= existing model preserved).

    In interactive mode, the user is expected to approve at startup via
    ``startup_guard`` prompts. Their decision is tracked in
    ``_session`` / ``_saved`` and consulted via
    ``_is_path_approved_for``. The new decl-consult check is gated on
    ``not self._interactive`` so interactive sessions retain the
    interactive-prompt-required posture (= user decline is honored).
    """
    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=True,
    )
    decl = PermissionDecl(
        file_write=[{"path": "*", "scope": "recursive"}],
    )
    out_of_zone = tmp_path.parent / "elsewhere" / "out.txt"
    with pytest.raises(PermissionError):
        resolver.require_file_write(decl, str(out_of_zone), "my_skill")


def test_require_file_read_interactive_mode_ignores_declaration(
    tmp_path: Path,
) -> None:
    """Tier 2: interactive mode does NOT consult decl for reads either (symmetry)."""
    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=True,
    )
    decl = PermissionDecl(
        file_read=[{"path": "*", "scope": "recursive"}],
    )
    out_of_zone = tmp_path.parent / "elsewhere" / "in.txt"
    with pytest.raises(PermissionError):
        resolver.require_file_read(decl, str(out_of_zone), "my_skill")


# Section 4: default zone + config-allow still take precedence -----------


def test_require_file_write_default_zone_passes_without_decl(tmp_path: Path) -> None:
    """Tier 2: default-write-zone paths (.reyn/ or reyn/) are granted without decl.

    Default-WRITE zones (per ``_DEFAULT_WRITE_ZONES``) are
    ``.reyn/`` and ``reyn/`` only -- not CWD broadly. Pre-PR-H
    rule preserved: paths inside those zones pass without any decl
    consultation.
    """
    import os
    os.chdir(tmp_path)
    (tmp_path / ".reyn").mkdir()
    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=False,
    )
    resolver.require_file_write(PermissionDecl(), ".reyn/state.json", "")


def test_require_file_write_config_allow_passes_without_decl(tmp_path: Path) -> None:
    """Tier 2: config-level allow grants regardless of decl (existing rule).

    The pre-PR-H rule order is preserved: ``_in_default_*_zone`` then
    ``_is_config_approved`` then ``_is_path_approved_for`` then the
    new decl-consult check. Config-level allow short-circuits before
    the new check is reached.
    """
    resolver = PermissionResolver(
        config_permissions={"file.write": "allow"},
        project_root=tmp_path,
        interactive=True,  # interactive but config-allow short-circuits
    )
    out_of_zone = tmp_path.parent / "elsewhere" / "x.txt"
    # No decl, no prompt -- config allow alone passes.
    resolver.require_file_write(PermissionDecl(), str(out_of_zone), "")
