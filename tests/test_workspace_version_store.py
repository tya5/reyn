"""Tier 2: OS invariant — WorkspaceVersionStore content-addressed shadow-git.

ADR-0038 Stage 1d (D9) + #1544 (async git-exec for container support). Real
``git`` + real filesystem (no mocks). The store is the workspace half of a
generation: capture the work-tree at a boundary seq, restore it as-of-N on
rewind. Git methods are async (#1544 — the container runner is async); git-absence
degrades at exec time. Covers the round-trip (files revert, later-added files
removed, excluded OS state survives), idempotent capture, nearest-at-or-below
restore, retention prune, and git-absent graceful degrade.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from reyn.core.events.workspace_version_store import WorkspaceVersionStore

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not on PATH",
)


def _store(tmp_path: Path) -> WorkspaceVersionStore:
    ws = tmp_path / "ws"
    (ws / ".reyn").mkdir(parents=True)
    return WorkspaceVersionStore(ws, ws / ".reyn" / "workspace-shadow.git")


def _write(tmp_path: Path, rel: str, content: str) -> None:
    p = tmp_path / "ws" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _read(tmp_path: Path, rel: str) -> str:
    return (tmp_path / "ws" / rel).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_capture_restore_round_trip(tmp_path):
    """Tier 2: restore reverts tracked files to as-of-N; later-added files gone; .reyn survives."""
    store = _store(tmp_path)
    _write(tmp_path, "file.txt", "v1")
    _write(tmp_path, ".reyn/wal.jsonl", "os-state")     # OS state — must NOT be tracked/wiped

    await store.capture(10)

    # later work: mutate a tracked file + add a new one
    _write(tmp_path, "file.txt", "v2")
    _write(tmp_path, "added.txt", "new")
    await store.capture(20)
    assert _read(tmp_path, "file.txt") == "v2"

    # rewind: restore to as-of-10
    await store.restore_at_or_below(10)

    assert _read(tmp_path, "file.txt") == "v1"               # reverted
    assert not (tmp_path / "ws" / "added.txt").exists()       # later-added removed
    assert _read(tmp_path, ".reyn/wal.jsonl") == "os-state"   # excluded OS state survives


@pytest.mark.asyncio
async def test_capture_is_idempotent_per_seq(tmp_path):
    """Tier 2: capturing the same seq twice returns the same sha (no duplicate gen).

    A global seq may be hit by more than one agent's boundary; the second
    capture must be a no-op returning the existing commit.
    """
    store = _store(tmp_path)
    _write(tmp_path, "file.txt", "v1")

    first = await store.capture(10)
    second = await store.capture(10)

    assert first is not None
    assert first == second
    assert await store.seqs() == [10]


@pytest.mark.asyncio
async def test_restore_picks_nearest_at_or_below(tmp_path):
    """Tier 2: restore_at_or_below(s) restores the nearest generation with seq <= s."""
    store = _store(tmp_path)
    _write(tmp_path, "file.txt", "gen10")
    await store.capture(10)
    _write(tmp_path, "file.txt", "gen20")
    await store.capture(20)

    await store.restore_at_or_below(15)   # between gens → nearest below = 10
    assert _read(tmp_path, "file.txt") == "gen10"


@pytest.mark.asyncio
async def test_restore_with_no_generation_returns_none(tmp_path):
    """Tier 2: restore with no captured generation is a safe no-op (returns None)."""
    store = _store(tmp_path)
    _write(tmp_path, "file.txt", "v1")
    assert await store.restore_at_or_below(5) is None
    assert _read(tmp_path, "file.txt") == "v1"   # untouched


@pytest.mark.asyncio
async def test_seqs_and_prune_below(tmp_path):
    """Tier 2: seqs() lists captured generations; prune_below drops older ones."""
    store = _store(tmp_path)
    _write(tmp_path, "file.txt", "v")
    for s in (10, 20, 30):
        _write(tmp_path, "file.txt", f"v{s}")
        await store.capture(s)
    assert await store.seqs() == [10, 20, 30]

    removed = await store.prune_below(20)
    assert removed == 1                 # only gen 10 dropped
    assert await store.seqs() == [20, 30]


@pytest.mark.asyncio
async def test_restore_to_seq_exact_and_missing(tmp_path):
    """Tier 2: restore_to_seq targets an EXACT generation; unknown seq is a no-op.

    This is the is_active-aware entry the registry uses with an active-resolved
    gen seq — it restores precisely that tag (never a nearest-below that could be
    an abandoned-branch gen).
    """
    store = _store(tmp_path)
    _write(tmp_path, "file.txt", "gen10")
    await store.capture(10)
    _write(tmp_path, "file.txt", "gen20")
    await store.capture(20)

    # exact restore to the older gen even though a newer one exists
    sha = await store.restore_to_seq(10)
    assert sha is not None
    assert _read(tmp_path, "file.txt") == "gen10"

    # unknown gen seq → safe no-op (workspace untouched)
    assert await store.restore_to_seq(999) is None
    assert _read(tmp_path, "file.txt") == "gen10"


@pytest.mark.asyncio
async def test_git_unavailable_degrades_to_noop(tmp_path, monkeypatch):
    """Tier 2: with no git on PATH, git-exec degrades at exec time to no-ops.

    #1544: degrade is exec-time (the runner raises GitUnavailable when the binary
    is missing), NOT a host-PATH pre-gate — so it's correct for container mode too.
    """
    monkeypatch.setenv("PATH", "")      # real env: no git resolvable
    store = _store(tmp_path)
    _write(tmp_path, "file.txt", "v1")

    assert store.host_git_available() is False   # host fast-path reflects it
    assert await store.capture(10) is None        # exec-time degrade
    assert await store.restore_at_or_below(10) is None
    assert await store.seqs() == []
