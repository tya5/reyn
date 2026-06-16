"""Tier 2: OS invariant — benchmark workspace chdir-race elimination (FP-0008 PR-I).

Verifies three invariants introduced by the PR-I root fix:

1. test_benchmark_workspace_does_not_mutate_cwd
   `_benchmark_isolated_workspace` does NOT call os.chdir; the global
   CWD before and after entering the context manager is identical.

2. test_concurrent_workspaces_see_distinct_base_dirs
   Four concurrent `_benchmark_isolated_workspace` contexts each yield a
   distinct temp directory path; the global CWD never changes during any
   of them (= no cross-coroutine CWD pollution).

3. test_workspace_constructor_honors_explicit_base_dir
   `Workspace(events=evt, base_dir=p)` sets `workspace.base_dir == p.resolve()`
   and does NOT read Path.cwd() when an explicit base_dir is provided.

No mocks (unittest.mock / AsyncMock / MagicMock / patch). Real Workspace
instances; benchmark context manager exercised directly.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.events.events import EventLog
from reyn.interfaces.cli.commands.eval_benchmark import _benchmark_isolated_workspace
from reyn.workspace.workspace import Workspace

# ── test 1: context manager does not mutate CWD ──────────────────────────────


def test_benchmark_workspace_does_not_mutate_cwd(tmp_path: Path) -> None:
    """Tier 2: _benchmark_isolated_workspace does not call os.chdir.

    The process-wide CWD must be the same before and after entering the
    context. This pins the PR-I invariant: eliminating the chdir race that
    caused 4 of 9 FP-0008 sandbox_2 v6 failures.
    """
    cwd_before = Path.cwd()

    with _benchmark_isolated_workspace(task=None, clone_task_repo=False) as ws:
        cwd_inside = Path.cwd()
        # ws is a real tempdir distinct from the original CWD
        assert ws.exists()
        assert ws != cwd_before

    cwd_after = Path.cwd()

    # Global CWD must be unchanged — never mutated during the context.
    assert cwd_inside == cwd_before, (
        f"CWD changed inside context: was {cwd_before}, saw {cwd_inside}"
    )
    assert cwd_after == cwd_before, (
        f"CWD changed after context: was {cwd_before}, now {cwd_after}"
    )


# ── test 2: concurrent contexts see distinct workspace paths ─────────────────


def test_concurrent_workspaces_see_distinct_base_dirs() -> None:
    """Tier 2: 4 concurrent _benchmark_isolated_workspace contexts yield distinct paths.

    With the chdir removed, each asyncio coroutine enters its own isolated
    tempdir context. No two tasks share a workspace path, and the global CWD
    is unchanged throughout (= no race between concurrent tasks).
    """
    cwd_before = Path.cwd()
    collected_paths: list[Path] = []

    async def _enter_and_record(task_id: int) -> Path:
        with _benchmark_isolated_workspace(task=None, clone_task_repo=False) as ws:
            # Simulate async work by yielding the event loop briefly
            await asyncio.sleep(0)
            return ws

    async def _run_concurrent() -> None:
        results = await asyncio.gather(*(_enter_and_record(i) for i in range(4)))
        collected_paths.extend(results)

    asyncio.run(_run_concurrent())

    # Every context yielded a real path
    assert collected_paths, "gather must yield at least one result"

    # All paths must be distinct (no two tasks shared a workspace)
    unique_paths = set(collected_paths)
    assert unique_paths == set(collected_paths), (
        f"Workspace paths must be distinct across concurrent tasks; got: {collected_paths}"
    )
    assert len(unique_paths) == len(collected_paths), (
        f"No two tasks may share a workspace path; got: {collected_paths}"
    )

    # Global CWD must be unchanged after all concurrent contexts completed
    cwd_after = Path.cwd()
    assert cwd_after == cwd_before, (
        f"CWD changed after concurrent contexts: was {cwd_before}, now {cwd_after}"
    )


# ── test 3: Workspace constructor honors explicit base_dir ───────────────────


def test_workspace_constructor_honors_explicit_base_dir(tmp_path: Path) -> None:
    """Tier 2: Workspace(events=evt, base_dir=p) uses p as base_dir, not Path.cwd().

    When an explicit base_dir is provided, the Workspace must NOT read the
    process-wide CWD. This is the mechanism that makes concurrent benchmark
    tasks safe: each task constructs a Workspace anchored to its own tempdir.
    """
    explicit_dir = tmp_path / "isolated_ws"
    explicit_dir.mkdir()

    events = EventLog(subscribers=[])
    ws = Workspace(events=events, base_dir=explicit_dir)

    # base_dir must equal the resolved explicit path
    assert ws.base_dir == explicit_dir.resolve(), (
        f"Expected base_dir={explicit_dir.resolve()}, got {ws.base_dir}"
    )

    # base_dir must NOT be the process CWD (they are distinct in this test)
    assert ws.base_dir != Path.cwd(), (
        "Workspace.base_dir must not default to CWD when explicit base_dir is provided"
    )
