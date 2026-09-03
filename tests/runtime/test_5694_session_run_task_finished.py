"""Tier 2: #5694 stage 2 (architect ruling, relayed by lead-coder-30) —
``AgentRegistry``'s own ``(name, sid)`` background ``session.run()`` task
had 4 creation sites and 0 ``add_done_callback`` registrations: a session
dying was consumed by nothing more specific than asyncio's own generic,
``(name, sid)``-blind unhandled-exception handler. This closes it: all 4
sites now funnel through :meth:`AgentRegistry._spawn_session_run`, whose
one done-callback (:meth:`AgentRegistry._on_session_run_task_done`)
durably records exactly which of 3 mutually-exclusive outcomes happened
(``completed`` / ``exception`` / ``cancelled``) plus ``(name, sid)``, via
a new ``session_run_task_finished`` audit-event kind.

Real ``AgentRegistry`` throughout — no mocks. Where the 3-way dispatch
itself is the subject, a synthetic coroutine is passed directly to
``_spawn_session_run`` (its own signature takes a coroutine, not a
``Session`` — see that method's own docstring for why): this is not
faking a collaborator, it is driving the dispatch logic with the
cheapest real input that exercises each of the 3 outcomes on demand,
same idiom as #5709's ``ProcessLoopBeatDriver`` tests injecting a fake
clock rather than waiting on a real one. The structural (deny-side) test
and the one full-stack test below use a REAL ``Session`` via
``make_session``/``ensure_running``/``shutdown``.

★ ``EventStore.write()`` (found while writing this test, measured
directly): even with ``emit_direct_event``'s own ``_force_inline=True``,
the actual DISK write moves off-loop onto a ``DurabilityWorker`` the
moment a loop is running (the JSON line is built synchronously; the
``open``/``write``/``fsync`` is fire-and-forget). So a file can exist
with ZERO lines for a brief window right after ``_on_session_run_task_
done`` returns. Every read below therefore polls unboundedly for content
(CLAUDE.md's own Ceiling rule — no fixed sleep/attempt count, pytest's
own ``--timeout`` is the kill switch) rather than reading once right
after ``await task``.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from tests._support.agent_session import make_session
from tests._support.paths import REPO_ROOT


def _registry(tmp_path: Path) -> AgentRegistry:
    shared = BudgetTracker(CostConfig())

    def factory(profile: AgentProfile):
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=shared,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    return AgentRegistry(project_root=tmp_path, session_factory=factory)


def _read_registry_events(tmp_path: Path) -> "list[dict]":
    """Read every event line under ``.reyn/events/direct/registry/`` —
    mirrors ``test_5065_permissions_router_emits_audit_event.py``'s own
    ``_read_direct_web_events`` helper, ``surface="registry"``."""
    registry_dir = tmp_path / ".reyn" / "events" / "direct" / "registry"
    if not registry_dir.is_dir():
        return []
    out: "list[dict]" = []
    for f in sorted(registry_dir.glob("*/*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


async def _wait_for_session_run_task_finished(tmp_path: Path, *, min_count: int = 1) -> "list[dict]":
    """Unbounded condition-wait (CLAUDE.md's own Ceiling rule — no fixed
    sleep/attempt count of our own; pytest's own ``--timeout`` is the kill
    switch) for at least *min_count* ``session_run_task_finished`` events
    to actually have landed on disk — see module docstring for why a
    single read right after ``await task`` can race the off-loop write."""
    while True:
        events = [
            e for e in _read_registry_events(tmp_path)
            if e.get("type") == "session_run_task_finished"
        ]
        if len(events) >= min_count:
            return events
        await asyncio.sleep(0)


# ── the 3-way dispatch, driven directly through _spawn_session_run ─────────


@pytest.mark.asyncio
async def test_a_normally_returning_coroutine_records_completed(tmp_path: Path) -> None:
    """Tier 2: the "completed" branch of the 3-way outcome dispatch."""
    reg = _registry(tmp_path)

    async def _noop() -> None:
        return None

    task = reg._spawn_session_run("agentX", "sidY", _noop())
    await task

    events = await _wait_for_session_run_task_finished(tmp_path)
    [event] = events  # exactly one callback firing for one task
    data = event["data"]
    assert data["name"] == "agentX"
    assert data["sid"] == "sidY"
    assert data["status"] == "completed"
    assert data["exception_type"] == ""
    assert data["exception_message"] == ""


@pytest.mark.asyncio
async def test_a_raising_coroutine_records_exception_type_and_message(tmp_path: Path) -> None:
    """Tier 2: the "exception" branch of the 3-way outcome dispatch.
    Also the witness that ``.exception()`` was actually retrieved — the
    event only carries these fields at all because
    ``_on_session_run_task_done`` called ``task.exception()``, which is
    what silences asyncio's own "exception was never retrieved" console
    warning (see that method's own docstring)."""
    reg = _registry(tmp_path)

    async def _boom() -> None:
        raise ValueError("kaboom")

    task = reg._spawn_session_run("agentX", "sidY", _boom())
    with pytest.raises(ValueError):
        await task  # the caller's own await still sees the raise -- retrieving
        # .exception() in the done-callback does not swallow it for OTHER readers

    events = await _wait_for_session_run_task_finished(tmp_path)
    [event] = events
    data = event["data"]
    assert data["status"] == "exception"
    assert data["exception_type"] == "ValueError"
    assert data["exception_message"] == "kaboom"


@pytest.mark.asyncio
async def test_a_cancelled_task_records_cancelled_with_no_exception_fields(tmp_path: Path) -> None:
    """Tier 2: the "cancelled" branch of the 3-way outcome dispatch —
    ``.cancelled()`` must be checked BEFORE ``.exception()`` (calling
    ``.exception()`` on a cancelled task raises ``CancelledError``)."""
    reg = _registry(tmp_path)

    async def _forever() -> None:
        await asyncio.sleep(3600)

    task = reg._spawn_session_run("agentX", "sidY", _forever())
    await asyncio.sleep(0)  # let it actually start before cancelling
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    events = await _wait_for_session_run_task_finished(tmp_path)
    [event] = events
    data = event["data"]
    assert data["status"] == "cancelled"
    assert data["exception_type"] == ""
    assert data["exception_message"] == ""


@pytest.mark.asyncio
async def test_the_event_lands_under_the_registrys_own_project_root_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5694 — ``AgentRegistry`` is exactly the long-lived-server
    shape ``emit_direct_event``'s own docstring names as wrong for
    ``emit_cli_event``'s ``Path.cwd()``-derived root. cwd is chdir'd
    somewhere ELSE entirely -- the event must still land under
    ``tmp_path`` (the registry's own ``project_root``), never under cwd."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    reg = _registry(tmp_path)

    async def _noop() -> None:
        return None

    await reg._spawn_session_run("agentX", "sidY", _noop())

    await _wait_for_session_run_task_finished(tmp_path)
    assert not (elsewhere / ".reyn" / "events").exists(), (
        "#5694 REGRESSION: event landed under cwd instead of the registry's "
        "own resolved project_root"
    )


# ── structural witness: exactly one creation site (deny-side) ──────────────


def test_registry_has_exactly_one_session_run_creation_site() -> None:
    """Tier 2: #5694's own accept criterion (①) — every ``session.run()``
    background task is created through ONE funnel. A second raw
    ``asyncio.create_task(<x>.run())`` anywhere else in this file would
    silently reopen the exact gap this issue closes (a creation site with
    no done-callback, consumed only by the generic handler). Mirrors
    #5709's own ``test_record_loop_beat_has_exactly_one_production_caller``
    git-grep shape.

    Real code never uses this file's own docstring markup (double
    backticks, ` `` `) around a code mention — every prose reference to
    ``asyncio.create_task(...)`` in a comment/docstring does, so filtering
    those lines out leaves only actual statements."""
    result = subprocess.run(
        ["git", "grep", "-n", "asyncio.create_task(", "--", "src/reyn/runtime/registry.py"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    code_lines = [
        line for line in result.stdout.splitlines()
        if "``" not in line  # excludes every prose/docstring mention
    ]
    assert code_lines, "sanity: registry.py must still create tasks via asyncio.create_task somewhere"
    run_call_lines = [line for line in code_lines if ".run()" in line]
    assert run_call_lines == [], (
        f"#5694 REGRESSION: a session.run() task is created via a direct "
        f"asyncio.create_task(<x>.run()) call outside _spawn_session_run "
        f"— {run_call_lines!r}"
    )


# ── full-stack: a real Session, via a real call site + real shutdown ───────


@pytest.mark.asyncio
async def test_a_real_session_shut_down_cooperatively_records_completed(tmp_path: Path) -> None:
    """Tier 2: end-to-end through the real production seam
    (``ensure_running`` -> ``AgentRegistry.shutdown()``'s cooperative
    sentinel path) -- ``Session.run_one_iteration`` returns ``False`` on
    the shutdown sentinel (no raise, no cancel), so the real task
    completes NORMALLY. Confirms the funnel is actually wired into a
    production call site, not just directly callable."""
    reg = _registry(tmp_path)
    await reg.ensure_running("default")
    await reg.shutdown()

    events = await _wait_for_session_run_task_finished(tmp_path)
    matching = [e for e in events if e["data"]["name"] == "default"]
    assert matching, "no session_run_task_finished event for the real 'default' session"
    assert matching[-1]["data"]["status"] == "completed", (
        f"a cooperative shutdown sentinel must complete normally, got {matching[-1]!r}"
    )
