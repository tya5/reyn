"""Tier 2: sub-skill phase events carry the child's run_id (issue #134).

When skill A spawns skill B via the ``run_skill`` Control IR op, B's
OSRuntime inherits A's subscriber list (= ``op_runtime/run_skill.py``
passes ``subscribers=ctx.subscribers`` so the chat outbox keeps receiving
trace updates from inside the sub-skill).  Pre-fix, every event flowing
through that shared ChatEventForwarder got stamped with the parent's
``run_id`` because the forwarder hardcoded ``self.run_id`` into outbox
metadata.  The TUI keyed SkillActivityRow by ``meta["run_id"]`` and
therefore wrote B's phase names into A's row.

The MVP fix pins three contracts:

  1. ``EventLog(run_id=...)`` stamps ``run_id`` into every emitted
     event's data (caller-wins convention mirrors agent_id).
  2. ``OSRuntime`` passes its own ``run_id`` when constructing
     ``EventLog`` so per-run identity flows with the event.
  3. ``ChatEventForwarder._enqueue`` prefers the event's own ``run_id``
     over the forwarder's, and stamps ``parent_run_id = self.run_id``
     when the two differ — letting downstream consumers attribute the
     event to the correct skill row.

TUI render-side improvements (nested rows, label prefixes) are a
separate follow-up; this test file pins the wire-level data only.
"""
from __future__ import annotations

import asyncio
from typing import Any

from reyn.chat.forwarder import ChatEventForwarder
from reyn.events.events import EventLog
from reyn.schemas.models import Event

# ── 1. EventLog stamps run_id ─────────────────────────────────────────────


def test_event_log_stamps_run_id() -> None:
    """Tier 2: EventLog(run_id='X') auto-injects 'run_id': 'X' into emit data."""
    log = EventLog(run_id="run-A")
    evt = log.emit("phase_started", phase="resolve")
    assert evt.data["run_id"] == "run-A"
    assert evt.data["phase"] == "resolve"


def test_event_log_caller_run_id_wins() -> None:
    """Tier 2: caller-supplied run_id in emit() takes precedence.

    Matches the existing agent_id contract — delegation flows can
    preserve the upstream origin's identity by passing run_id explicitly.
    """
    log = EventLog(run_id="forwarder-run")
    evt = log.emit("phase_started", phase="resolve", run_id="explicit-run")
    assert evt.data["run_id"] == "explicit-run"


def test_event_log_no_run_id_unchanged() -> None:
    """Tier 2: when EventLog has no run_id, emit data is not augmented.

    Backward-compat: every existing test that constructs EventLog
    without ``run_id`` continues to emit without a run_id key.
    """
    log = EventLog()
    evt = log.emit("phase_started", phase="resolve")
    assert "run_id" not in evt.data


# ── 2. ChatEventForwarder routes by event's run_id ────────────────────────


def _drain(q: asyncio.Queue) -> list[Any]:
    items: list[Any] = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def test_forwarder_uses_event_run_id_when_present() -> None:
    """Tier 2: event.data.run_id wins over forwarder's run_id (= sub-skill case).

    Child skill emitting through a shared forwarder must NOT clobber
    its identity with the parent's run_id.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("router", q, run_id="parent-run")
    fwd(Event(type="phase_started", data={"phase": "code_review", "run_id": "child-run"}))
    (msg,) = _drain(q)
    assert msg.meta["run_id"] == "child-run"
    assert msg.meta["run_id_short"] == "-run"


def test_forwarder_stamps_parent_run_id_when_differs() -> None:
    """Tier 2: when event and forwarder run_id differ, parent_run_id is set.

    Issue #134 contract: TUI consumers read parent_run_id to render
    nested rows / lineage hints.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("router", q, run_id="parent-run")
    fwd(Event(type="phase_started", data={"phase": "p", "run_id": "child-run"}))
    msgs = _drain(q)
    assert msgs[0].meta["parent_run_id"] == "parent-run"


def test_forwarder_no_parent_run_id_when_same_run() -> None:
    """Tier 2: when event run_id == forwarder run_id, parent_run_id is omitted.

    Same-run events do not need the nesting marker — only cross-run
    events do.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("s", q, run_id="run-A")
    fwd(Event(type="phase_started", data={"phase": "p", "run_id": "run-A"}))
    msgs = _drain(q)
    assert "parent_run_id" not in msgs[0].meta


def test_forwarder_falls_back_to_self_run_id_when_event_lacks() -> None:
    """Tier 2: events without run_id fall back to forwarder's run_id.

    Backward-compat: existing test sites that emit raw events without
    run_id continue to land on the forwarder's run_id (= prior behavior).
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("s", q, run_id="run-A")
    fwd(Event(type="phase_started", data={"phase": "p"}))
    msgs = _drain(q)
    assert msgs[0].meta["run_id"] == "run-A"
    assert "parent_run_id" not in msgs[0].meta


def test_forwarder_act_executed_routes_by_event_run_id() -> None:
    """Tier 2: detail-emitting handlers also honor the event's run_id.

    The on_act_executed handler must thread source_run_id through
    _enqueue so detail rows attach to the correct skill row.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("router", q, run_id="parent-run")
    fwd(Event(
        type="act_executed",
        data={"op_count": 2, "run_id": "child-run"},
    ))
    msgs = _drain(q)
    assert msgs[0].meta["run_id"] == "child-run"
    assert msgs[0].meta["parent_run_id"] == "parent-run"


def test_forwarder_workflow_finished_routes_by_event_run_id() -> None:
    """Tier 2: workflow terminal events also honor the event's run_id.

    Sub-skill completion must land on the child row, not the parent's.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("router", q, run_id="parent-run")
    fwd(Event(type="workflow_finished", data={"run_id": "child-run"}))
    msgs = _drain(q)
    assert msgs[0].meta["run_id"] == "child-run"
    assert msgs[0].meta["parent_run_id"] == "parent-run"
