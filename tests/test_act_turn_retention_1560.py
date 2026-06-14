"""Tier 2: act-turn op-tree retention via gc-root refs (#1560 PR-3).

The per-op `write-tree` snapshots (PR-1) are bare tree objects — unreachable, so
git auto-gc would reclaim them even in-window and break restore. PR-3 pins each as
a gc-root (`refs/reyn-op/<op_seq>`) while in-window, drops the ref at retention,
and lets auto-gc reclaim the now-unreachable out-window trees (the same bounded
lifecycle generations get via their commit chain). These pin the load-bearing
property: **in-window op-trees survive `git gc`; out-window ones (ref dropped) are
reclaimed**. Real git + AgentRegistry + store, no mocks.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.events.state_log import StateLog
from reyn.events.workspace_op_content_log import WorkspaceOpContentLog
from reyn.events.workspace_version_store import _OP_REF_PREFIX, WorkspaceVersionStore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git required")


def _git(git_dir: Path, work_tree: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "--git-dir", str(git_dir), "--work-tree", str(work_tree), *args],
        capture_output=True, text=True,
    )


# ── store: ref gc-protection (the correctness floor) ───────────────────────


@pytest.mark.asyncio
async def test_op_ref_protects_tree_from_gc(tmp_path) -> None:
    """Tier 2: a ref'd op-tree survives an aggressive `git gc --prune=now`
    (in-window protection); the bare unreffed tree would be reclaimed."""
    git_dir = tmp_path / ".reyn" / "workspace-shadow.git"
    store = WorkspaceVersionStore(tmp_path, git_dir)
    (tmp_path / "code.py").write_text("v1", encoding="utf-8")

    tree = await store.capture_tree()
    await store.ref_op_tree(100, tree)
    assert _git(git_dir, tmp_path, "rev-parse", f"{_OP_REF_PREFIX}100").stdout.strip() == tree

    _git(git_dir, tmp_path, "gc", "--prune=now")                 # aggressive gc
    assert _git(git_dir, tmp_path, "cat-file", "-t", tree).stdout.strip() == "tree"  # survived


@pytest.mark.asyncio
async def test_unref_below_then_gc_reclaims_out_window(tmp_path) -> None:
    """Tier 2: unref_op_trees_below drops out-window refs → those trees become
    unreachable + auto-gc reclaims them, while in-window refs (+ trees) survive."""
    git_dir = tmp_path / ".reyn" / "workspace-shadow.git"
    store = WorkspaceVersionStore(tmp_path, git_dir)

    (tmp_path / "code.py").write_text("old", encoding="utf-8")
    tree_old = await store.capture_tree()
    await store.ref_op_tree(100, tree_old)
    (tmp_path / "code.py").write_text("new", encoding="utf-8")
    tree_new = await store.capture_tree()
    await store.ref_op_tree(200, tree_new)

    dropped = await store.unref_op_trees_below(150)             # floor between 100 and 200
    assert dropped == 1
    assert _git(git_dir, tmp_path, "rev-parse", "--verify", f"{_OP_REF_PREFIX}100").returncode != 0
    assert _git(git_dir, tmp_path, "rev-parse", f"{_OP_REF_PREFIX}200").stdout.strip() == tree_new

    _git(git_dir, tmp_path, "gc", "--prune=now")
    assert _git(git_dir, tmp_path, "cat-file", "-t", tree_old).returncode != 0   # reclaimed
    assert _git(git_dir, tmp_path, "cat-file", "-t", tree_new).stdout.strip() == "tree"  # kept


@pytest.mark.asyncio
async def test_generation_prune_unaffected(tmp_path) -> None:
    """Tier 2: generation prune (tag -d) still works alongside the op-ref additions
    — the existing boundary-generation retention is unchanged."""
    git_dir = tmp_path / ".reyn" / "workspace-shadow.git"
    store = WorkspaceVersionStore(tmp_path, git_dir)
    (tmp_path / "code.py").write_text("v1", encoding="utf-8")
    await store.capture(50)
    assert 50 in await store.seqs()
    await store.prune_below(100)
    assert 50 not in await store.seqs()                         # gen-tag pruned as before


# ── op-content-log entry prune ─────────────────────────────────────────────


def test_op_content_log_prune_below(tmp_path) -> None:
    """Tier 2: entry-drop keeps only op_seq >= floor (index half of retention)."""
    log = WorkspaceOpContentLog(tmp_path / "op-content-log.jsonl")
    log.append(100, "a")
    log.append(200, "b")
    log.append(300, "c")
    assert log.prune_below(200) == 1
    assert [e["op_seq"] for e in log.entries()] == [200, 300]


# ── registry: capture observer pins the ref end-to-end ─────────────────────


@pytest.mark.asyncio
async def test_capture_observer_pins_op_ref(tmp_path) -> None:
    """Tier 2: with act_turn_capture on, a `step_completed` append records the
    op-content-log entry AND pins the gc-root ref (the full capture wiring)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile):
        raise AssertionError("factory must not be called")

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
        workspace_capture=True, act_turn_capture=True,
    )
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    (tmp_path / "code.py").write_text("v1", encoding="utf-8")

    seq = await state_log.append("step_completed", run_id="r1", op_kind="file_write")

    captured = {e["op_seq"]: e["tree_sha"] for e in reg.op_content_log.entries()}
    assert seq in captured                                      # entry recorded
    git_dir = tmp_path / ".reyn" / "workspace-shadow.git"
    ref = _git(git_dir, tmp_path, "rev-parse", f"{_OP_REF_PREFIX}{seq}")
    assert ref.returncode == 0 and ref.stdout.strip() == captured[seq]   # ref pins the tree
