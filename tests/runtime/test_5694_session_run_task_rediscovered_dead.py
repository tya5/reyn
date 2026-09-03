"""Tier 2: #5694 stage 2 disposition (architect ruling, relayed by
lead-coder-30) — the arc's own final open item ("処分"). The existing
per-``(name, sid)`` restart-guard (`key not in self._tasks or self.
_tasks[key].done()`) already restarts a dead session's run-task, request-
driven, every time — that was never in question. What was missing: no
record that this happened. #5694's own incident took 2 sessions to
diagnose because the death was consumed purely as a restart trigger and
left nothing behind.

This closes it with ONE additional fact, emitted from the SAME place
that decides "reuse or restart": ``AgentRegistry._ensure_session_run``
(the direct continuation of #5715's own ``_spawn_session_run``
consolidation) now emits ``session_run_task_rediscovered_dead`` exactly
when it finds a PRIOR task for ``(name, sid)`` already ``done()`` —
never on a genuine first boot. Deliberately no new policy: no retry
count, no backoff, no crash-loop detection, no push notification (the
ruling's own explicit "落とすもの（全部）" list) — read-only, via
``reyn doctor`` / fleet visibility, same as ``session_run_task_finished``
(#5715).

Real ``AgentRegistry`` + real ``Session`` throughout — no mocks. Reuses
the ``_registry``/``_wait_for_...``/``_read_registry_events`` idioms
from ``test_5694_session_run_task_finished.py`` (same off-loop
``EventStore`` write race applies here — see that file's own module
docstring for why every read below polls unboundedly rather than
reading once).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from tests._support.agent_session import make_session


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


def _rediscovery_events(tmp_path: Path) -> "list[dict]":
    return [
        e for e in _read_registry_events(tmp_path)
        if e.get("type") == "session_run_task_rediscovered_dead"
    ]


async def _wait_for_rediscovery_events(tmp_path: Path, *, min_count: int = 1) -> "list[dict]":
    """Unbounded condition-wait (CLAUDE.md's own Ceiling rule) for at
    least *min_count* rediscovery events to have actually landed on
    disk — the off-loop ``EventStore`` write race, see module docstring."""
    while True:
        events = _rediscovery_events(tmp_path)
        if len(events) >= min_count:
            return events
        await asyncio.sleep(0)


# ── the 2-way dispatch: first-boot vs. rediscovery ──────────────────────────


@pytest.mark.asyncio
async def test_first_boot_emits_no_rediscovery_event(tmp_path: Path) -> None:
    """Tier 2: `key` absent from `self._tasks` is a first boot, never a
    rediscovery — the deny side of the ruling's own condition."""
    reg = _registry(tmp_path)
    session = reg.get_or_load("default")

    task = reg._ensure_session_run("default", "main", session)
    await asyncio.sleep(0)  # let it actually start
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert _rediscovery_events(tmp_path) == [], (
        "#5694 REGRESSION: a first boot must never emit "
        "session_run_task_rediscovered_dead"
    )


@pytest.mark.asyncio
async def test_a_still_running_task_is_reused_with_no_rediscovery_event(tmp_path: Path) -> None:
    """Tier 2: calling ``_ensure_session_run`` again while the previous
    task is still alive (not done) must reuse it — no new task, no
    rediscovery event. Mirrors #5709 R5's own "1 process + 2 Sessions =
    1 beater" idempotency shape, one layer down."""
    reg = _registry(tmp_path)
    session = reg.get_or_load("default")

    task1 = reg._ensure_session_run("default", "main", session)
    await asyncio.sleep(0)
    assert not task1.done()

    task2 = reg._ensure_session_run("default", "main", session)
    assert task2 is task1, "a still-running task must be reused, never replaced"

    task1.cancel()
    try:
        await task1
    except asyncio.CancelledError:
        pass

    assert _rediscovery_events(tmp_path) == []


@pytest.mark.asyncio
async def test_rediscovering_a_done_task_emits_the_event_with_name_and_sid(tmp_path: Path) -> None:
    """Tier 2: the accept criterion itself — a PRIOR task for `(name, sid)`
    that is already `done()` when `_ensure_session_run` is called again
    is a genuine rediscovery, and must emit exactly once with the right
    identity."""
    reg = _registry(tmp_path)
    session = reg.get_or_load("default")

    task1 = reg._ensure_session_run("default", "main", session)
    task1.cancel()
    try:
        await task1
    except asyncio.CancelledError:
        pass
    assert task1.done()

    task2 = reg._ensure_session_run("default", "main", session)
    assert task2 is not task1, "a done task must be REPLACED, not reused"

    events = await _wait_for_rediscovery_events(tmp_path)
    [event] = events
    data = event["data"]
    assert data["name"] == "default"
    assert data["sid"] == "main"

    task2.cancel()
    try:
        await task2
    except asyncio.CancelledError:
        pass


# ── full-stack: a real Session, via the real ensure_running() call site ────


@pytest.mark.asyncio
async def test_a_real_session_shut_down_then_ensure_running_again_rediscovers_it(
    tmp_path: Path,
) -> None:
    """Tier 2: end-to-end through the real production seam. ``Session.
    shutdown()`` stops JUST that session's own run-loop (unlike
    ``AgentRegistry.shutdown()``, which stops every loaded session) — the
    task completes normally and stays in `self._tasks` (nothing pops
    it), so a SECOND `ensure_running("default")` finds it `done()` and
    rediscovers it, exactly the shape a real detach-then-message-arrives
    incident produces."""
    reg = _registry(tmp_path)
    session = await reg.ensure_running("default")
    await session.shutdown()

    while not reg._tasks[("default", "main")].done():
        await asyncio.sleep(0)

    await reg.ensure_running("default")

    events = await _wait_for_rediscovery_events(tmp_path)
    matching = [e for e in events if e["data"]["name"] == "default"]
    assert matching, "no session_run_task_rediscovered_dead event for the real 'default' session"
