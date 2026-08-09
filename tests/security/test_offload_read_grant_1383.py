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
  - the grant is unaffected by a `sandbox_policy` argument (#3901 PR-B ③
    retired FILE_READ from the SandboxLayer permission-∩ projection).

#2396 Step 4: the two ``context_builder`` emit points that used to wire
``on_offload_ref=r.grant_offload_read`` (``maybe_ref_artifact`` and
``offload_control_ir_result``) were both dead in production (their only caller,
the ContextFrame-driven phase path, was removed by earlier convergence steps)
and were deleted along with the rest of the DICT-offload machinery. These
tests now exercise `grant_offload_read` / the read gates directly — the
generic primitive survives and remains available for a future offload
producer to wire.

No mocks: a real PermissionResolver with project_root ≠ the offloaded path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.security.sandbox.policy import SandboxPolicy


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


@pytest.mark.asyncio
async def test_out_of_zone_read_denied_without_grant(tmp_path: Path) -> None:
    """Tier 2c: baseline — an out-of-zone offload path is denied with no grant (the 13236 bug)."""
    r, offloaded, _ = _out_of_zone(tmp_path)
    with pytest.raises(PermissionError):
        await r.require_file_read(PermissionDecl(), str(offloaded), "swe_bench")


@pytest.mark.asyncio
async def test_grant_offload_read_allows_exact_path(tmp_path: Path) -> None:
    """Tier 2c: after grant_offload_read, the exact out-of-zone path is readable."""
    r, offloaded, _ = _out_of_zone(tmp_path)
    r.grant_offload_read(str(offloaded))
    await r.require_file_read(PermissionDecl(), str(offloaded), "swe_bench")  # no raise


@pytest.mark.asyncio
async def test_grant_is_exact_scoped_not_sibling(tmp_path: Path) -> None:
    """Tier 2c: the grant is exact-path scoped — a SIBLING in the same dir stays denied
    (least-privilege: granting one offloaded artifact does NOT open the state-dir)."""
    r, offloaded, state = _out_of_zone(tmp_path)
    r.grant_offload_read(str(offloaded))
    sibling = state / "other_secret.json"
    sibling.write_text("{}")
    with pytest.raises(PermissionError):
        await r.require_file_read(PermissionDecl(), str(sibling), "swe_bench")


@pytest.mark.asyncio
async def test_register_reaches_check_seam(tmp_path: Path) -> None:
    """Tier 2c: register→check wiring falsification — the SAME resolver instance that
    registered the grant is the one the read gate consults. Without the register the
    read fails; with it, it passes. (A grant stored where the check never reads it
    would leave this denied = unwired bug.)"""
    r, offloaded, _ = _out_of_zone(tmp_path)
    # before register: denied
    with pytest.raises(PermissionError):
        await r.require_file_read(PermissionDecl(), str(offloaded), "swe_bench")
    # register on this instance → the check (same instance) now passes
    r.grant_offload_read(str(offloaded))
    await r.require_file_read(PermissionDecl(), str(offloaded), "swe_bench")


@pytest.mark.asyncio
async def test_offload_grant_survives_a_sandbox_policy_argument(tmp_path: Path) -> None:
    """Tier 2c: the offload grant admits the read regardless of what
    ``sandbox_policy`` is passed alongside it.

    #3901 PR-B ③ retired FILE_READ from ``SandboxLayer``'s permission-∩
    projection (an operator cannot know a sandbox's path floor, so it is no
    longer treated as permission — see effective.py's own docstring); before
    PR-B this test pinned the OPPOSITE claim (a sandbox read-restriction still
    denied an offload-granted path). That intersection is gone by design, not
    by regression — this witnesses the grant is unaffected by whatever
    ``sandbox_policy`` a caller passes."""
    r, offloaded, _ = _out_of_zone(tmp_path)
    r.grant_offload_read(str(offloaded))
    policy = SandboxPolicy(write_paths=[str(tmp_path / "elsewhere")])
    await r.require_file_read(
        PermissionDecl(), str(offloaded), "swe_bench", sandbox_policy=policy
    )


# ── #1383 follow-up: the Workspace read gate (is_read_allowed) must honor the
# grant too — the op-runtime gate (require_file_read) passing alone left
# astropy-13236 still aborting at Workspace._resolve_read ("outside project").

def test_is_read_allowed_honors_offload_grant(tmp_path: Path) -> None:
    """Tier 2c: is_read_allowed (the Workspace read gate's resolver method) honors the
    offload grant — denied before, allowed after, on the same instance."""
    r, offloaded, _ = _out_of_zone(tmp_path)
    assert r.is_read_allowed(str(offloaded), "swe_bench") is False  # before grant
    r.grant_offload_read(str(offloaded))
    assert r.is_read_allowed(str(offloaded), "swe_bench") is True  # after grant


@pytest.mark.asyncio
async def test_both_read_gates_in_sync_for_offload_grant(tmp_path: Path) -> None:
    """Tier 2c: the two read gates make the SAME decision for an offloaded path —
    require_file_read (op-runtime) and is_read_allowed (Workspace) both admit it
    after the grant. (The follow-up restores the symmetry the merged D12 broke by
    patching only require_file_read.)"""
    r, offloaded, _ = _out_of_zone(tmp_path)
    r.grant_offload_read(str(offloaded))
    # op-runtime gate
    await r.require_file_read(PermissionDecl(), str(offloaded), "swe_bench")  # no raise
    # Workspace gate
    assert r.is_read_allowed(str(offloaded), "swe_bench") is True


@pytest.mark.asyncio
async def test_read_gates_symmetric_granted_and_nongranted(tmp_path: Path) -> None:
    """Tier 2c: symmetry-invariant (falsifies future divergence) — for BOTH a
    granted offload path AND a non-granted out-of-zone path, require_file_read and
    is_read_allowed return the SAME admit/deny. They share `_read_base_approved`
    for the config+offload decision, so neither can drift on the offload axis."""
    r, offloaded, state = _out_of_zone(tmp_path)
    nongranted = state / "not_granted.json"

    async def _require_ok(p: Path) -> bool:
        try:
            await r.require_file_read(PermissionDecl(), str(p), "swe_bench")
            return True
        except PermissionError:
            return False

    # non-granted out-of-zone: both DENY (symmetric)
    assert await _require_ok(nongranted) is False
    assert r.is_read_allowed(str(nongranted), "swe_bench") is False
    # granted: both ALLOW (symmetric)
    r.grant_offload_read(str(offloaded))
    assert await _require_ok(offloaded) is True
    assert r.is_read_allowed(str(offloaded), "swe_bench") is True


def test_is_read_allowed_grant_is_exact_scoped(tmp_path: Path) -> None:
    """Tier 2c: is_read_allowed's offload grant is exact-path scoped — a sibling in the
    same state-dir stays disallowed (least-privilege, matching the op-runtime gate)."""
    r, offloaded, state = _out_of_zone(tmp_path)
    r.grant_offload_read(str(offloaded))
    sibling = state / "other_secret.json"
    assert r.is_read_allowed(str(sibling), "swe_bench") is False


def test_workspace_read_gate_honors_offload_grant_end_to_end(tmp_path: Path) -> None:
    """Tier 2c: end-to-end through the real Workspace — `Workspace.read_file` of an
    offloaded out-of-zone path raises 'outside project' before the grant and succeeds
    after (this is the exact gate-2 that left 13236 aborting)."""
    from reyn.core.events.events import EventLog
    from reyn.data.workspace.workspace import Workspace

    r, offloaded, _ = _out_of_zone(tmp_path)
    offloaded.write_text("INPUT-CONTENT")
    base = (tmp_path / "proj").resolve()
    ws = Workspace(
        EventLog(), permission_resolver=r, actor="swe_bench", base_dir=base
    )
    with pytest.raises(PermissionError):
        ws.read_file(str(offloaded))  # gate-2: outside project, no grant
    r.grant_offload_read(str(offloaded))
    content, found = ws.read_file(str(offloaded))
    assert found and content == "INPUT-CONTENT"
