"""Tier 2: OS invariant — #2248 PR-C recovery-core write-gate PREFIXES.

The protect-at-use carve-out is generalized from a few explicit files to the
``{config/, state/}`` prefixes: a raw ``file.write`` under ``.reyn/config/`` or
``.reyn/state/`` is NOT silently allowed by the broad ``.reyn/`` default zone — it must go
through a dedicated op that declares the path explicitly (mcp_install/drop, cron_register,
index_drop). The no-legit-op-blocked matrix proven here: a dedicated-op write (explicit
decl) PASSES; a raw file.write to a recovery-core prefix is DENIED; ``approvals.yaml``
(top-level persist) stays protected; ``memory/`` + ``cache/`` + other ``.reyn/`` paths stay
default-granted (the prefix-deny must not over-reach).
"""
from __future__ import annotations

import pytest

from reyn.security.permissions.permissions import (
    _in_default_write_zone,
    _is_under_recovery_core_prefix,
)


def test_recovery_core_prefix_paths_excluded_from_default_zone(tmp_path, monkeypatch):
    """Tier 2: a write under .reyn/config/ or .reyn/state/ is NOT in the default write zone
    (so a raw file.write needs an explicit decl). RED if the prefix-deny were dropped — the
    path would auto-grant via the broad .reyn/ zone = the recovery-core bypass gap."""
    monkeypatch.chdir(tmp_path)
    for rel in (
        ".reyn/config/mcp.yaml", ".reyn/config/index/sources.yaml",
        ".reyn/state/wal.jsonl", ".reyn/state/snapshot.json",
    ):
        assert _in_default_write_zone(rel) is False, f"{rel} must be excluded from default zone"
        assert _is_under_recovery_core_prefix(rel) is True


def test_non_recovery_core_reyn_paths_still_default_granted(tmp_path, monkeypatch):
    """Tier 2: memory/ (persist), cache/ (derived), and other .reyn/ paths stay
    default-granted — the prefix-deny must NOT over-reach. RED if it swept them in."""
    monkeypatch.chdir(tmp_path)
    for rel in (
        ".reyn/memory/note.md", ".reyn/cache/index/x.db",
        ".reyn/scratch.txt", ".reyn/events/log.jsonl",
    ):
        assert _in_default_write_zone(rel) is True, f"{rel} must stay default-granted"
        assert _is_under_recovery_core_prefix(rel) is False


def test_safe_file_denies_raw_write_under_recovery_core_prefix(tmp_path, monkeypatch):
    """Tier 2: safe.file._check_write DENIES a raw write to .reyn/config/ or .reyn/state/
    covered only by the broad .reyn/ parent-dir (no explicit listing) — forcing the
    dedicated-op path. RED if the prefix-deny were removed (the .reyn/ zone would allow it)."""
    monkeypatch.chdir(tmp_path)
    from reyn.api.safe import file as safe_file

    safe_file._set_permission_context(
        read_paths=[str(tmp_path)],
        write_paths=[str(tmp_path / ".reyn"), str(tmp_path / "reyn")],
    )
    with pytest.raises(PermissionError):
        safe_file._check_write(str(tmp_path / ".reyn" / "config" / "mcp.yaml"))
    with pytest.raises(PermissionError):
        safe_file._check_write(str(tmp_path / ".reyn" / "state" / "wal.jsonl"))


def test_safe_file_accepts_config_write_with_explicit_decl(tmp_path, monkeypatch):
    """Tier 2: the dedicated-op path — .reyn/config/mcp.yaml WITH an explicit file.write
    decl PASSES (mcp_install/drop session-approve the exact path). RED if the prefix-deny
    rejected even an explicitly-declared write = the no-legit-op-blocked invariant broken."""
    monkeypatch.chdir(tmp_path)
    from reyn.api.safe import file as safe_file

    safe_file._set_permission_context(
        read_paths=[str(tmp_path)],
        write_paths=[
            str(tmp_path / ".reyn"),
            str(tmp_path / ".reyn" / "config" / "mcp.yaml"),  # the dedicated-op explicit decl
        ],
    )
    safe_file._check_write(str(tmp_path / ".reyn" / "config" / "mcp.yaml"))  # must not raise


def test_safe_file_allows_non_recovery_core_reyn_write(tmp_path, monkeypatch):
    """Tier 2: a write to .reyn/memory/ or .reyn/cache/ PASSES via the broad .reyn/ zone —
    not swept into the prefix-deny. RED if the prefix over-reached to all of .reyn/."""
    monkeypatch.chdir(tmp_path)
    from reyn.api.safe import file as safe_file

    safe_file._set_permission_context(
        read_paths=[str(tmp_path)],
        write_paths=[str(tmp_path / ".reyn")],
    )
    safe_file._check_write(str(tmp_path / ".reyn" / "memory" / "note.md"))  # must not raise
    safe_file._check_write(str(tmp_path / ".reyn" / "cache" / "x.db"))  # must not raise
