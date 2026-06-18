"""Tier 2: plan_step threads from planner → spawned skill → forwarder (#214).

Pre-fix, a skill spawned via ``invoke_skill`` inside a plan step's
sub-``RouterLoop`` had no way to know it belonged to "step N of M" —
the SkillActivityRow showed ``⠋ skill_name#abcd · phase · 2.1s`` with no
plan context. Per the owner decision direction (ii), this PR threads
``plan_step`` through:

  planner (ContextVar setter) →
    skill_runner (ContextVar reader) →
    SkillRuntime.run / sub_skill_runner.invoke_sub_skill →
    OSRuntime / EventLog (caller-wins auto-injection mirror of run_id) →
    ChatEventForwarder (one-shot ``detail: plan N/M`` on first phase_started)

This file pins each link of the chain:

  1. ``EventLog(plan_step=...)`` auto-injects into emit data.
  2. ``current_plan_step()`` returns the contextvar's current value.
  3. ``set_plan_step()`` sets + resets within the with-block scope.
  4. ``ChatEventForwarder.on_phase_started`` emits a one-shot
     ``detail: plan N/M`` when the event carries ``plan_step``.
  5. Once-per-run-id de-duplication (= subsequent phase_started events
     for the same run_id do NOT re-emit the plan detail).

End-to-end planner integration is exercised by the existing
``test_plan_multi_plan_resume_race.py`` framework, which is out of
scope here — this file pins the unit invariants of each link.
"""
from __future__ import annotations

import asyncio
from typing import Any

from reyn.core.events.events import EventLog
from reyn.runtime.forwarder import ChatEventForwarder
from reyn.schemas.models import Event
from reyn.skill._plan_step_context import current_plan_step, set_plan_step


def _drain(q: asyncio.Queue) -> list[Any]:
    items: list[Any] = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


# ── 1. EventLog stamps plan_step ──────────────────────────────────────────


def test_event_log_stamps_plan_step() -> None:
    """Tier 2: EventLog(plan_step={...}) auto-injects plan_step into emit data."""
    log = EventLog(plan_step={"n_done": 2, "n_total": 3, "step_id": "s2"})
    evt = log.emit("phase_started", phase="resolve")
    assert evt.data["plan_step"] == {"n_done": 2, "n_total": 3, "step_id": "s2"}


def test_event_log_caller_plan_step_wins() -> None:
    """Tier 2: caller-supplied plan_step takes precedence (= same pattern as run_id)."""
    log = EventLog(plan_step={"n_done": 1, "n_total": 2, "step_id": "s1"})
    evt = log.emit(
        "phase_started",
        phase="resolve",
        plan_step={"n_done": 99, "n_total": 99, "step_id": "explicit"},
    )
    assert evt.data["plan_step"]["step_id"] == "explicit"


def test_event_log_no_plan_step_unchanged() -> None:
    """Tier 2: EventLog without plan_step does not inject the key.

    Backward-compat: top-level (non-plan) invocations land with no
    plan_step in event data, same as pre-#214.
    """
    log = EventLog()
    evt = log.emit("phase_started", phase="resolve")
    assert "plan_step" not in evt.data


# ── 2. ContextVar helper ───────────────────────────────────────────────────


def test_current_plan_step_default_is_none() -> None:
    """Tier 2: outside any set_plan_step block, current_plan_step() is None."""
    assert current_plan_step() is None


def test_set_plan_step_scopes_to_with_block() -> None:
    """Tier 2: set_plan_step() context manager sets + resets the var."""
    assert current_plan_step() is None
    with set_plan_step(n_done=2, n_total=5, step_id="s2") as payload:
        assert current_plan_step() == payload
        assert payload["n_done"] == 2
        assert payload["n_total"] == 5
        assert payload["step_id"] == "s2"
    assert current_plan_step() is None


def test_set_plan_step_nested_blocks_restore_outer() -> None:
    """Tier 2: nested set_plan_step blocks restore outer scope on exit."""
    with set_plan_step(n_done=1, n_total=3, step_id="outer"):
        assert current_plan_step()["step_id"] == "outer"
        with set_plan_step(n_done=2, n_total=3, step_id="inner"):
            assert current_plan_step()["step_id"] == "inner"
        # Inner with-block exited — outer scope restored.
        assert current_plan_step()["step_id"] == "outer"
    assert current_plan_step() is None


# ── 3. Forwarder emits "plan N/M" detail on first phase_started ──────────


def test_forwarder_emits_plan_detail_on_first_phase_started() -> None:
    """Tier 2: phase_started with plan_step → "detail: plan N/M" trace.

    The detail trace lands BEFORE the phase_started trace so the row's
    set_detail call fires on row mount. ConversationView's existing
    detail-routing path picks it up as `⤷ plan N/M` under the phase name.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("child", q, run_id="parent-run")
    fwd(Event(
        type="phase_started",
        data={
            "phase": "resolve",
            "run_id": "child-run",
            "plan_step": {"n_done": 2, "n_total": 3, "step_id": "s2"},
        },
    ))
    msgs = _drain(q)
    # plan detail first, then phase started.
    assert any(m.text == "detail: plan 2/3" for m in msgs)
    assert msgs[0].text == "detail: plan 2/3"
    assert msgs[0].meta["run_id"] == "child-run"
    assert msgs[1].text == "phase started: resolve"


def test_forwarder_plan_detail_only_once_per_run_id() -> None:
    """Tier 2: subsequent phase_started for same run_id does NOT re-emit detail.

    De-dup discipline so the row's detail doesn't get clobbered by
    repeated "plan N/M" every phase advance — in-phase signals
    (on_llm_called / on_act_executed) own the detail post-mount.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("child", q, run_id="parent-run")
    plan_step = {"n_done": 2, "n_total": 3, "step_id": "s2"}
    for phase in ("resolve", "execute", "synthesize"):
        fwd(Event(
            type="phase_started",
            data={
                "phase": phase, "run_id": "child-run", "plan_step": plan_step,
            },
        ))
    msgs = _drain(q)
    plan_detail_msgs = [m for m in msgs if "plan 2/3" in m.text]
    # de-dup: exactly one detail, not duplicated on each phase transition
    assert plan_detail_msgs, "plan detail must fire for child run_id"
    assert sum(1 for m in msgs if "plan 2/3" in m.text) < 2, (
        "plan detail must not repeat per child run_id"
    )


def test_forwarder_skips_plan_detail_when_event_lacks_plan_step() -> None:
    """Tier 2: event without plan_step → no detail emit (= backward-compat).

    Top-level skill invocations / non-plan flows have plan_step=None
    on EventLog construction. Forwarder must not synthesize a fake
    "plan ?" line.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("s", q, run_id="r")
    fwd(Event(type="phase_started", data={"phase": "p", "run_id": "r"}))
    msgs = _drain(q)
    # No plan detail line prepended — only the phase_started trace.
    assert not any("plan" in m.text and m.text.split(": ", 1)[0] not in ("plan",) and "/" in m.text for m in msgs)
    assert any(m.text == "phase started: p" for m in msgs)


def test_forwarder_distinct_run_ids_each_get_their_own_detail() -> None:
    """Tier 2: two different child run_ids each get their own one-shot detail.

    Confirms the de-dup tracking is keyed by run_id (= different
    spawned skills inside the same step both get marked correctly).
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("step", q, run_id="parent-run")
    plan_step = {"n_done": 2, "n_total": 3, "step_id": "s2"}
    fwd(Event(type="phase_started", data={
        "phase": "p", "run_id": "child-A", "plan_step": plan_step,
    }))
    fwd(Event(type="phase_started", data={
        "phase": "p", "run_id": "child-B", "plan_step": plan_step,
    }))
    msgs = _drain(q)
    plan_detail_msgs = [m for m in msgs if "plan 2/3" in m.text]
    assert {m.meta["run_id"] for m in plan_detail_msgs} == {"child-A", "child-B"}
