"""Tier 2: OS invariant — #2248 PR-C recovery-core write-gate PREFIXES.

The protect-at-use carve-out is generalized from a few explicit files to the
``{config/, state/}`` prefixes: a raw ``file.write`` under ``.reyn/config/`` or
``.reyn/state/`` is NOT silently allowed by the broad ``.reyn/`` default zone — it must go
through a dedicated op that declares the path explicitly (mcp_install/drop, cron_register,
index_drop). The no-legit-op-blocked matrix proven here: a dedicated-op write (explicit
decl) PASSES; a raw file.write to a recovery-core prefix is DENIED; ``approvals.yaml``
(top-level persist) stays protected; ``memory/`` + ``cache/`` + other ``.reyn/`` paths stay
default-granted (the prefix-deny must not over-reach).

#5238 (TMPDIR-dependent anchor audit, full count in the issue): of this file's 6
tests, 2 pin their own ``reyn.yaml`` into ``tmp_path`` below (they call
``reyn.api.safe.file``'s WALK-based ``_project_root_for_gate()``, which a
``TMPDIR`` placing ``tmp_path`` inside an outer ``reyn.yaml`` tree would
otherwise mis-anchor). The other 4 do not, and do not need to:
``test_recovery_core_prefix_paths_excluded_from_default_zone`` /
``test_non_recovery_core_reyn_paths_still_default_granted`` call
``reyn.security.permissions.permissions``'s SAME-NAMED sibling functions, which
take a plain ``Path.cwd()`` base with no walk at all — immune by construction.
``test_safe_file_allows_non_recovery_core_reyn_write`` targets
``.reyn/memory/``/``.reyn/cache/``, never in the protected/recovery-core lists
under ANY root. ``test_safe_file_recovery_core_gate_anchors_on_project_root_
not_launch_subdir`` already pins its own ``reyn.yaml`` (it is the pattern the
2 fixed tests below now follow).
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
    dedicated-op path. RED if the prefix-deny were removed (the .reyn/ zone would allow it).

    #5238: ``_check_write`` anchors via ``_project_root_for_gate()``, which walks UP
    from cwd looking for ``reyn.yaml`` — pinning one directly in ``tmp_path`` (same
    pattern as ``test_safe_file_recovery_core_gate_anchors_on_project_root_not_
    launch_subdir`` below) makes the walk stop here regardless of whatever
    ``reyn.yaml`` may or may not exist further up under ``TMPDIR``. Without this, a
    ``TMPDIR`` that places ``tmp_path`` inside an ancestor's own ``reyn.yaml`` tree
    (real incident, coder-smith, 2026-08-24) anchors the gate on that OUTER
    project instead — this test's own ``.reyn/config``/``.reyn/state`` targets then
    fall outside the (wrongly-anchored) recovery-core prefix, the raw write is
    wrongly ALLOWED, and this test's own ``pytest.raises`` fails to see a raise."""
    (tmp_path / "reyn.yaml").write_text("mcp:\n  servers: {}\n", encoding="utf-8")
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
    rejected even an explicitly-declared write = the no-legit-op-blocked invariant broken.

    #5238: pinned anchor (see the sibling deny-test's docstring) — this closes a
    real vacuous-pass, not just a hypothetical one: WITHOUT the pin, a ``TMPDIR``
    that anchors the gate on an outer project makes ``_is_under_recovery_core_
    prefix`` return False before the explicit-decl branch this test claims to
    exercise is ever reached — the assertion below ("must not raise") was true
    either way, so the test passed while checking nothing. STRIP-FALSIFY
    (performed, not left as a claim): with the anchor pinned, removing the
    explicit ``.reyn/config/mcp.yaml`` decl from ``write_paths`` makes this
    correctly go RED (``PermissionError``, the no-legit-op-blocked invariant this
    test exists to prove) — confirmed manually, reverted before commit."""
    (tmp_path / "reyn.yaml").write_text("mcp:\n  servers: {}\n", encoding="utf-8")
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


def test_safe_file_recovery_core_gate_anchors_on_project_root_not_launch_subdir(
    tmp_path, monkeypatch,
):
    """Tier 2: #4204 bucket C — the recovery-core write-gate must anchor on the
    PROJECT root (walked up via reyn.yaml), not the raw process cwd, so a
    ``reyn`` launched from a subdirectory still gates the REAL
    ``.reyn/config``/``.reyn/state`` — not a phantom
    ``<subdir>/.reyn/config`` that doesn't exist, and not silently letting
    the real path through unguarded.

    STRIP-FALSIFY: replacing ``_project_root_for_gate()`` with a bare
    ``os.getcwd()`` (the pre-#4204 form) makes this go RED — the gate
    would anchor at ``<project>/subdir/.reyn/config``, which does not
    prefix-match the real target path under ``<project>/.reyn/config``, so
    ``_is_under_recovery_core_prefix`` returns False and the raw write is
    wrongly ALLOWED via the broad ``.reyn/`` zone."""
    (tmp_path / "reyn.yaml").write_text("mcp:\n  servers: {}\n", encoding="utf-8")
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    monkeypatch.chdir(subdir)  # the operator launched reyn from here, not tmp_path

    from reyn.api.safe import file as safe_file

    safe_file._set_permission_context(
        read_paths=[str(tmp_path)],
        write_paths=[str(tmp_path / ".reyn"), str(tmp_path / "reyn")],
    )
    # The REAL recovery-core path (under the project root, not the launch
    # subdirectory) must still be gated — a raw write is DENIED.
    with pytest.raises(PermissionError):
        safe_file._check_write(str(tmp_path / ".reyn" / "config" / "mcp.yaml"))
