"""Tier 2: #5350 — process identity is answered from a RECORDED fact
(``process_registry``'s own ``agent_name``/``broker_session_id`` fields),
never derived from ``cwd``.

Real incident (owner-observed, 2026-08-30, relayed by lead-coder): an
operator-side script treated "same ``cwd`` as a registered agent" as
"is this agent" and sent ``SIGTERM`` to unrelated ``zsh``/``nvim``
processes that merely happened to share a directory with a reyn agent.
Architect ruling (#5350): ``cwd`` never carries identity — a process
must record its OWN name (it already knows it; no guessing), and a
reader (:func:`process_registry.process_for_agent`) must filter by that
recorded field, never by position.

Real, separate OS subprocesses throughout (never a synthetic marker
faked in-process for a PID that isn't actually running that code) —
mirrors ``test_5226_process_registry.py``'s own convention, including
its no-sleep/no-duration release-by-closing-stdin idiom (CLAUDE.md's own
Ceiling/Floor rule) and its own ``out_of_process_reyn`` (#5028) pin —
each subprocess imports ``reyn``, so its ``PYTHONPATH`` is pinned to the
SAME checkout this test itself imports, never left to the ambient
venv/worktree to resolve on its own.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reyn.runtime import process_registry


@pytest.fixture(autouse=True)
def _isolated_processes_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Mirrors test_5226_process_registry.py's own fixture — the real
    ``~/.reyn/processes/`` is shared, user-global state and must never
    be touched by a test."""
    processes_dir = tmp_path / "processes"
    monkeypatch.setattr(process_registry, "PROCESSES_DIR", processes_dir)
    return processes_dir


def _spawn_identified_subprocess(
    *, cwd: Path, agent_name: "str | None", pythonpath: str,
) -> subprocess.Popen:
    """A REAL, separate OS subprocess that registers its own real PID,
    then (if *agent_name* is given) records its own identity — mirrors
    ``test_5226_process_registry.py``'s own ``_spawn_registering_
    subprocess``, extended to also exercise :func:`record_process_
    identity`. Blocks on a real stdin read (never a sleep) until the
    test releases it by closing stdin.

    *pythonpath* is the ``out_of_process_reyn`` fixture's own value
    (#5028) — pinned as this subprocess's ``PYTHONPATH`` so it imports
    the SAME ``reyn`` this test itself imported, rather than trusting
    the ambient venv/worktree to agree."""
    record_line = (
        f"process_registry.record_process_identity(agent_name={agent_name!r})\n"
        if agent_name is not None else ""
    )
    script = (
        "import sys\n"
        "from reyn.runtime import process_registry\n"
        "process_registry.PROCESSES_DIR = __import__('pathlib').Path(sys.argv[1])\n"
        "process_registry.register_process('chat')\n"
        f"{record_line}"
        "sys.stdin.readline()\n"  # blocks for real — released by closing stdin
    )
    env = {**os.environ, "PYTHONPATH": pythonpath}
    return subprocess.Popen(
        [sys.executable, "-c", script, str(process_registry.PROCESSES_DIR)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=str(cwd), env=env,
    )


def _wait_until(condition) -> None:
    """CLAUDE.md's own Ceiling rule: poll unboundedly, no sleep-based
    fixed wait — pytest's/CI's own --timeout is the kill switch."""
    while not condition():
        time.sleep(0)  # yield the GIL, not a duration this assertion depends on


def test_process_for_agent_ignores_a_same_cwd_process_recorded_under_a_different_name(
    tmp_path: Path, out_of_process_reyn: str,
) -> None:
    """Tier 2: #5350 accept — two REAL processes share the exact same
    ``cwd`` (the incident's own shape: multiple processes in one
    directory). Only ONE recorded itself as ``"target-agent"``; the
    other recorded a DIFFERENT name (standing in for an unrelated
    process that happens to share the directory — zsh/nvim in the real
    incident never call ``record_process_identity`` at all, but a
    same-cwd SIBLING reyn process with a different name is the sharper
    proof: it IS a real marker, IS in ``live_processes()``, and would
    still be wrongly matched by any cwd-based join).

    Both the deny side (the wrong-named process is NEVER returned) and
    the accept side (the right one IS, per architect's own instruction
    not to leave this deny-only) are in this one test."""
    shared_cwd = tmp_path / "shared-workdir"
    shared_cwd.mkdir()
    target = _spawn_identified_subprocess(
        cwd=shared_cwd, agent_name="target-agent", pythonpath=out_of_process_reyn,
    )
    other = _spawn_identified_subprocess(
        cwd=shared_cwd, agent_name="other-agent", pythonpath=out_of_process_reyn,
    )
    try:
        _wait_until(lambda: len(process_registry.live_processes()) == 2)
        markers = process_registry.live_processes()
        assert {m["cwd"] for m in markers} == {str(shared_cwd)}, (
            "test setup sanity: both processes must share the exact same "
            "cwd, or this test proves nothing about cwd-based mis-join"
        )

        matched = process_registry.process_for_agent("target-agent")

        # Accept side: the right process IS found.
        matched_pids = {m["pid"] for m in matched}
        assert matched_pids == {target.pid}, (
            f"expected process_for_agent('target-agent') to return exactly "
            f"the process that recorded that name (pid {target.pid}), got "
            f"{matched_pids!r}"
        )
        # Deny side: the SAME-cwd, differently-named process is never
        # included — this is what would go red if the lookup joined on
        # cwd instead of the recorded agent_name field.
        assert other.pid not in matched_pids, (
            f"#5350 REGRESSION: a same-cwd process recorded under a "
            f"DIFFERENT agent_name (pid {other.pid}) was returned by "
            f"process_for_agent('target-agent') — this is the exact "
            f"cwd-mis-join shape the real incident produced"
        )
    finally:
        for proc in (target, other):
            if proc.stdin is not None:
                proc.stdin.close()
        for proc in (target, other):
            proc.wait()


def test_a_process_that_never_recorded_an_identity_is_never_matched(
    tmp_path: Path, out_of_process_reyn: str,
) -> None:
    """Tier 2: #5350 — a registered reyn process that never called
    ``record_process_identity`` (``agent_name`` stays the ``None``
    ``register_process`` itself writes) is never returned by ANY
    ``process_for_agent`` query — ``None`` is not a wildcard."""
    proc = _spawn_identified_subprocess(
        cwd=tmp_path, agent_name=None, pythonpath=out_of_process_reyn,
    )
    try:
        _wait_until(lambda: len(process_registry.live_processes()) == 1)
        assert process_registry.process_for_agent("target-agent") == [], (
            "an unidentified process (agent_name still None) must never "
            "match any real agent_name query"
        )
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        proc.wait()
