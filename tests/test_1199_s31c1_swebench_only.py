"""Tier 2: S3.1c-1 — non-interactive decl auto-grant removed; divergence resolved.

#1199 S3.1c-1 makes the op-runtime file gates (require_file_read/write) decl-less:
a path is permitted iff in the default zone OR approved. The skill's declared
paths are NO LONGER auto-granted in non-interactive mode. This:
  - denies a non-interactive declared-but-unapproved out-of-zone path (the
    tightening; decision-enabling deny message),
  - leaves interactive + in-zone + config-approved paths working,
  - resolves the S3.1b-2 divergence: require_file_* and is_*_allowed now make
    the SAME decision (the op-runtime gate no longer honored decls the Workspace
    gate ignored).

swe_bench (out-of-zone file.read/write: "*", non-interactive) is the sole real
dependent of the old auto-grant; it now works via a config-grant
(permissions.file.*: allow), which the eval benchmark injects for its isolated run.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from tests.test_permissions import _make_resolver

# swe_bench-style out-of-zone declaration.
_STAR_READ = PermissionDecl(file_read=[{"path": "*", "scope": "recursive"}])
_STAR_WRITE = PermissionDecl(file_write=[{"path": "*", "scope": "recursive"}])


def _out_of_zone(tmp_path: Path) -> str:
    # #1316: the resolver's project_root IS tmp_path (see _make_resolver), and the
    # default READ zone is "any path under project_root" — so a path UNDER tmp_path
    # is in-zone. A genuinely out-of-zone path (out of both the read zone = under
    # project_root and the write zone = project_root/.reyn|reyn) must live OUTSIDE
    # project_root. (Pre-#1316 the zone fns hardcoded cwd, so a tmp_path-internal
    # path looked out-of-zone only because tmp_path != cwd — that divergence is the
    # bug #1316 fixes.) A sibling of project_root is outside it.
    return str(tmp_path.parent / "out_of_zone_sibling" / "f.txt")


def test_non_interactive_declared_unapproved_read_denies_with_message(tmp_path: Path) -> None:
    """Tier 2: a non-interactive declared-but-unapproved out-of-zone read denies
    with a decision-enabling message (what / why / options)."""
    r = _make_resolver(tmp_path)  # non-interactive, no config approvals
    path = _out_of_zone(tmp_path)
    with pytest.raises(PermissionError) as exc:
        r.require_file_read(_STAR_READ, path, "swe_bench")
    msg = str(exc.value)
    assert "not approved" in msg
    assert "reyn.yaml" in msg          # pre-approve option
    assert "interactively" in msg      # run-interactively option


def test_non_interactive_declared_unapproved_write_denies_with_message(tmp_path: Path) -> None:
    """Tier 2: same for write."""
    r = _make_resolver(tmp_path)
    path = _out_of_zone(tmp_path)
    with pytest.raises(PermissionError) as exc:
        r.require_file_write(_STAR_WRITE, path, "swe_bench")
    msg = str(exc.value)
    assert "not approved" in msg
    assert "reyn.yaml" in msg
    assert "interactively" in msg


def test_interactive_declared_unapproved_still_denies(tmp_path: Path) -> None:
    """Tier 2: interactive mode is unchanged — it never auto-granted declared
    paths either (the gate is decl-less; startup-guard approval is the path).
    An out-of-zone declared path with no approval still denies at the gate."""
    r = PermissionResolver(config_permissions={}, project_root=tmp_path, interactive=True)
    path = _out_of_zone(tmp_path)
    with pytest.raises(PermissionError):
        r.require_file_write(_STAR_WRITE, path, "swe_bench")


def test_swebench_works_via_config_grant(tmp_path: Path) -> None:
    """Tier 2: with the config-grant (file.read/write: allow) the eval benchmark
    injects, swe_bench's out-of-zone "*" ops are approved (no prompt)."""
    r = _make_resolver(tmp_path, config={"file.read": "allow", "file.write": "allow"})
    path = _out_of_zone(tmp_path)
    # Neither raises = approved via config.
    r.require_file_read(_STAR_READ, path, "swe_bench")
    r.require_file_write(_STAR_WRITE, path, "swe_bench")


def test_in_zone_paths_unaffected(tmp_path: Path) -> None:
    """Tier 2: in-zone paths pass regardless of decl (eval_builder / ops_report /
    index skills' relative + .reyn/ paths are unaffected by the auto-grant removal)."""
    r = _make_resolver(tmp_path)
    # Write zone = .reyn/ + reyn/ under cwd; read zone = under cwd. Use relative
    # paths so they resolve under the repo cwd (the default zones).
    r.require_file_write(PermissionDecl(), ".reyn/index/events/index.db", "index_events")
    r.require_file_read(PermissionDecl(), "src/reyn/stdlib/skills/eval_builder/skill.md", "eval_builder")


@pytest.mark.parametrize(
    "config",
    [{}, {"file.read": "allow", "file.write": "allow"}],
)
def test_divergence_resolved_gates_agree(tmp_path: Path, config: dict) -> None:
    """Tier 2: ★require_file_* and is_*_allowed make the SAME decision (the
    S3.1b-2 divergence is resolved). For an out-of-zone declared-unapproved path
    both DENY (config={}) and both ALLOW (config grants) — real resolver."""
    r = _make_resolver(tmp_path, config=config)
    path = _out_of_zone(tmp_path)

    # is_*_allowed → bool
    read_allowed = r.is_read_allowed(path, "swe_bench")
    write_allowed = r.is_write_allowed(path, "swe_bench")

    # require_file_* → raises iff not allowed
    def _require_ok(fn, decl) -> bool:
        try:
            fn(decl, path, "swe_bench")
            return True
        except PermissionError:
            return False

    require_read_ok = _require_ok(r.require_file_read, _STAR_READ)
    require_write_ok = _require_ok(r.require_file_write, _STAR_WRITE)

    # Same decision (this is the divergence resolution — pre-S3.1c-1 the
    # require_file_* gate honored the "*" decl non-interactively while
    # is_*_allowed did not, so they would DISAGREE for config={}).
    assert read_allowed == require_read_ok
    assert write_allowed == require_write_ok
    # Sanity: empty config → both deny; granting config → both allow.
    expected = bool(config)
    assert read_allowed is expected
    assert write_allowed is expected
