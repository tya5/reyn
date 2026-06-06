"""Tier 2c: scoped read-grant for OS-offloaded artifacts (#1383 D12).

The OS offloads a large artifact to a state-dir path and hands the agent an
`artifact_ref` / `_offload_ref` pointing there, instructing it (llm.py) to
`file.read` that path. But the path is outside the default read zone (CWD /
project_root), so without a grant the agent is told to read a path it is then
denied — the #183/#1375 swe_bench loop's astropy-13236 burned its act budget
retrying the denied read and aborted.

Fix: the offload-emit registers a scoped read-grant on EXACTLY that path
(`grant_offload_read`), consulted by the read gate. These tests pin:
  - the grant admits the exact path but NOT a sibling (least-privilege),
  - the register→check seam (no grant → still denied = the grant is what passes),
  - the emit→grant→check chain end-to-end for BOTH emit points
    (maybe_ref_artifact + offload_control_ir_result),
  - the AgentLayer grant still ∩-intersects the SandboxLayer.

No mocks: a real PermissionResolver with project_root ≠ the offloaded path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.context_builder import maybe_ref_artifact, offload_control_ir_result
from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.sandbox.policy import SandboxPolicy


def _resolver(project_root: Path) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={}, project_root=project_root, interactive=False
    )


def _out_of_zone(tmp_path: Path) -> tuple[PermissionResolver, Path, Path]:
    """A resolver whose project_root is `proj`, plus an offload path OUTSIDE it."""
    proj = (tmp_path / "proj").resolve()
    proj.mkdir(parents=True)
    state = (tmp_path / "state").resolve()  # state-dir, outside the read zone
    state.mkdir(parents=True)
    offloaded = state / "v01_input.json"
    offloaded.write_text("{}")
    return _resolver(proj), offloaded, state


def test_out_of_zone_read_denied_without_grant(tmp_path: Path) -> None:
    """Tier 2c: baseline — an out-of-zone offload path is denied with no grant (the 13236 bug)."""
    r, offloaded, _ = _out_of_zone(tmp_path)
    with pytest.raises(PermissionError):
        r.require_file_read(PermissionDecl(), str(offloaded), "swe_bench")


def test_grant_offload_read_allows_exact_path(tmp_path: Path) -> None:
    """Tier 2c: after grant_offload_read, the exact out-of-zone path is readable."""
    r, offloaded, _ = _out_of_zone(tmp_path)
    r.grant_offload_read(str(offloaded))
    r.require_file_read(PermissionDecl(), str(offloaded), "swe_bench")  # no raise


def test_grant_is_exact_scoped_not_sibling(tmp_path: Path) -> None:
    """Tier 2c: the grant is exact-path scoped — a SIBLING in the same dir stays denied
    (least-privilege: granting one offloaded artifact does NOT open the state-dir)."""
    r, offloaded, state = _out_of_zone(tmp_path)
    r.grant_offload_read(str(offloaded))
    sibling = state / "other_secret.json"
    sibling.write_text("{}")
    with pytest.raises(PermissionError):
        r.require_file_read(PermissionDecl(), str(sibling), "swe_bench")


def test_register_reaches_check_seam(tmp_path: Path) -> None:
    """Tier 2c: register→check wiring falsification — the SAME resolver instance that
    registered the grant is the one the read gate consults. Without the register the
    read fails; with it, it passes. (A grant stored where the check never reads it
    would leave this denied = unwired bug.)"""
    r, offloaded, _ = _out_of_zone(tmp_path)
    # before register: denied
    with pytest.raises(PermissionError):
        r.require_file_read(PermissionDecl(), str(offloaded), "swe_bench")
    # register on this instance → the check (same instance) now passes
    r.grant_offload_read(str(offloaded))
    r.require_file_read(PermissionDecl(), str(offloaded), "swe_bench")


def test_emit_artifact_ref_registers_grant_end_to_end(tmp_path: Path) -> None:
    """Tier 2c: the artifact_ref emit point (maybe_ref_artifact) → grant → check chain.
    A large artifact emits an artifact_ref AND the path becomes readable via the grant."""
    r, offloaded, _ = _out_of_zone(tmp_path)
    big = {"type": "swe_bench_input", "data": {"problem_statement": "x" * 200_000}}
    out = maybe_ref_artifact(
        big, str(offloaded), on_offload_ref=r.grant_offload_read
    )
    assert out["type"] == "artifact_ref"  # large → offloaded to a ref
    # the emit registered the grant → the agent can read what it was told to read
    r.require_file_read(PermissionDecl(), str(offloaded), "swe_bench")


def test_emit_control_ir_offload_registers_grant_end_to_end(tmp_path: Path) -> None:
    """Tier 2c: the generic offload emit point (offload_control_ir_result) → grant → check.
    Covers the second of the two exhaustive emit points."""
    r, _, state = _out_of_zone(tmp_path)
    big_result = {"content": "y" * 200_000}
    inline = offload_control_ir_result(
        big_result, 0, state, cap=1024, on_offload_ref=r.grant_offload_read
    )
    ref_path = inline["_offload_ref"]
    r.require_file_read(PermissionDecl(), str(ref_path), "swe_bench")  # granted → no raise


def test_offload_grant_still_intersects_sandbox(tmp_path: Path) -> None:
    """Tier 2c: the AgentLayer offload grant is conjunctive-∩ with the SandboxLayer —
    a sandbox that restricts reads to other paths still denies the offloaded path."""
    r, offloaded, _ = _out_of_zone(tmp_path)
    r.grant_offload_read(str(offloaded))
    # SandboxLayer with a non-empty read allowlist that excludes the offloaded path
    policy = SandboxPolicy(read_paths=[str(tmp_path / "elsewhere")])
    with pytest.raises(PermissionError):
        r.require_file_read(
            PermissionDecl(), str(offloaded), "swe_bench", sandbox_policy=policy
        )
