"""Tier 2: Windows subprocess-cancel tree-kill (#2292) + CodeAct no-killpg guard (#2715).

The shared cancel/timeout reaper ``kill_process_tree`` must kill the child TREE with a
platform-correct mechanism: ``os.killpg`` on POSIX (whole process group), and on Windows —
where ``os.killpg`` / process groups don't exist — ``taskkill /T`` (whole descendant tree).
``proc.terminate()`` / ``proc.kill()`` alone reach ONLY the direct process, orphaning
grandchildren (#2292). Separately, the CodeAct runner used ``os.killpg`` with NO guard →
``AttributeError`` on any no-``killpg`` platform (#2715); it now routes through the same
guarded reaper.

The Windows branch is SELECTED whenever ``os.killpg`` is absent — a condition we simulate
cross-platform by deleting ``killpg`` from ``os`` (monkeypatch). We then assert, on the dev
env, that (a) the tree-kill helper FIRES (the branch is taken) and (b) the CodeAct kill path
no longer raises ``AttributeError``. Real subprocesses + the real reaper throughout — the
spy records the module's own tree-kill call and delegates to it (not a collaborator mock).
The actual Windows tree-REAPING (grandchildren gone) is a logical property of ``taskkill /T``
targeting the tree; the owner's real-Windows run is a sanity check, not the correctness gate.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from reyn.core.kernel import codeact_runner
from reyn.security.sandbox import _subprocess_io


@pytest.mark.asyncio
async def test_no_killpg_selects_tree_kill_not_just_direct(monkeypatch):
    """Tier 2: with os.killpg absent (Windows), kill_process_tree takes the tree-kill branch —
    ``taskkill /T`` (whole tree) FIRES, not just proc.terminate() on the direct process (#2292).

    RED if the Windows branch omitted the tree-kill: the spy would never record a call, i.e. only
    the direct process would be signalled and grandchildren would be orphaned."""
    calls: list[tuple[int, bool]] = []
    real = _subprocess_io._taskkill_tree

    def spy(pid: int, *, force: bool) -> None:
        calls.append((pid, force))
        return real(pid, force=force)  # delegate to the real reaper (no mock)

    monkeypatch.setattr(_subprocess_io, "_taskkill_tree", spy)
    monkeypatch.delattr(_subprocess_io.os, "killpg", raising=False)

    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    await _subprocess_io.kill_process_tree(p, grace_seconds=1.0)

    assert p.poll() is not None, "the process must still be killed on the no-killpg path"
    assert calls, "the Windows branch MUST invoke the tree-kill (taskkill /T), not just terminate"
    assert calls[0][0] == p.pid, "taskkill must target the spawned process's pid (the tree root)"


@pytest.mark.asyncio
async def test_codeact_timeout_kill_no_attributeerror_without_killpg(monkeypatch):
    """Tier 2: the CodeAct runner's timeout-kill path is guarded against a missing os.killpg (#2715).

    On a no-killpg platform the old unguarded ``os.killpg(...)`` raised AttributeError (RED),
    crashing timeout cleanup. Routed through the shared guarded reaper, the timeout branch now
    returns a clean ``status='timeout'`` envelope instead of raising."""
    monkeypatch.delattr(_subprocess_io.os, "killpg", raising=False)

    async def dispatch(name: str, args: dict) -> dict:
        return {"status": "ok", "data": None}

    runner = codeact_runner.CodeActRunner()
    # A snippet that never returns → forces the timeout branch (→ kill_process_tree). No imports
    # needed (the restricted namespace need not grant any module for a bare loop).
    out = await runner.run(
        code="while True:\n    pass",
        dispatch=dispatch,
        allow_unsandboxed=True,
        timeout=0.5,
    )

    assert out["status"] == "timeout", out
    assert out["ok"] is False, out
