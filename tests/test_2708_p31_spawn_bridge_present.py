"""#2708 P3.1 — a chat-invoked pipeline's ``present`` reaches the PARENT by construction.

P3.1 replaces the #2707 interim outbox-forward with a STRUCTURAL fix: the attached
pipeline driver-session is spawned with a ``SpawnBridgePresentationConsumer`` bound to the
PARENT (caller) session, so the driver's present sink IS the parent's sink. Two halves:

  * Half-A (visible output, type-level by-construction): a ``present`` step's render
    reaches the parent chat surface EXACTLY ONCE, and NOT via the driver's own outbox
    (the driver never renders to itself — its sink is the parent's). The removed #2707
    forward + the bridge would double-deliver, so single-delivery is the guard.
  * Half-B (audit, mechanism-level): the driver's ``presented`` P6 audit event is bridged
    onto the PARENT's EventLog with ``bridged_from=<driver_sid>`` provenance — the #2570
    driver→parent EventLog bridge extended to carry ``presented`` (it previously carried
    only ``pipeline_step_*``, leaving the present audit trail split in the driver log).

Scope (P3-item3 completeness gate, NOT P3.1): the DETACHED/async path
(``start_pipeline_run``) has no attached parent surface, so its present is intentionally
NOT bridged in P3.1 — a latent known-RED cell tracked separately, asserted here so it is
explicit, not silently broken.

Real ``AgentRegistry``/``Session``/``StateLog``/``PipelineExecutor``/present op — no
collaborator mocks. Asserts on the public outbox + EventLog surfaces, never Rich
formatting/whitespace (a Tier-4 pin).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.config_recovery import reyn_root
from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import Pipeline, ToolStep
from reyn.core.pipeline.work_order import pipeline_run_dir, read_result
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_api import run_pipeline_attached, start_pipeline_run
from reyn.runtime.session_params import PresentationWiring
from tests._support.agent_session import make_session

_MARKER = "REYN2708P31BRIDGEMARKER"


def _agent_registry(tmp_path: Path, state_log: "StateLog") -> AgentRegistry:
    """Real AgentRegistry + real Session factory that ACCEPTS the P3.1 present-sink
    override (the widened factory protocol): the attached driver spawn threads a
    parent-bound consumer through ``presentation_consumer=`` and the factory forwards it,
    so the driver's present renders to the parent's outbox by construction."""
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        return make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            presentation_wiring=PresentationWiring(presentation_consumer=presentation_consumer, intervention_bridge=intervention_bridge),
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    if not reg.exists("worker"):
        reg.create("worker")
    return reg


def _present_pipeline() -> Pipeline:
    return Pipeline(steps=[
        ToolStep(name="present", args={"data_inline": {"label": _MARKER}}, output="ack"),
    ])


def _drain(queue: "asyncio.Queue") -> list:
    out: list = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def _presentations(msgs: list) -> list:
    return [m for m in msgs if getattr(m, "kind", None) == "presentation"]


def _driver_sid_from(events) -> "str | None":
    for e in events.all():
        if e.type == "pipeline_run_attached":
            return e.data.get("driver_sid")
    return None


@pytest.mark.asyncio
async def test_attached_present_reaches_parent_by_construction_single_delivery(
    tmp_path: Path,
) -> None:
    """Tier 2: Half-A — a sync-attached ``run_pipeline_attached`` present lands on the
    CALLER (parent) outbox EXACTLY ONCE via the inherited sink, and the DRIVER's own
    outbox holds ZERO presentation (proves the sink is parent-bound, not a
    self-render-then-forward). RED if the #2707 forward is resurrected alongside the
    bridge (double delivery → the single-presentation unpack fails)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log)
    caller = reg.get_or_load("worker")

    outcome = await run_pipeline_attached(
        reg,
        pipeline=_present_pipeline(),
        pipeline_name="shows",
        input=None,
        reply_to_agent="worker",
        reply_to_sid="main",
        state_log=state_log,
        tool="run_pipeline",
        caller_events=caller.router_host.events,
    )
    assert outcome["status"] == "ok"

    driver_sid = _driver_sid_from(caller.router_host.events)
    assert driver_sid is not None

    # Parent surface: exactly one presentation, carrying the marker.
    caller_pres = _presentations(_drain(caller.outbox))
    (presented,) = caller_pres  # fails RED on 0 (orphan) or >1 (double delivery)
    assert _MARKER in json.dumps(presented.meta)

    # The driver session did NOT render to its own outbox (its sink is the parent's).
    driver = reg.get_session("worker", driver_sid)
    assert driver is not None
    assert _presentations(_drain(driver.outbox)) == [], (
        "the driver-session rendered a presentation to its OWN outbox — the spawn-bridge "
        "sink should have routed it to the parent (a self-render would be the #2707 "
        "forward-era shape, an orphan without the removed forward)."
    )


@pytest.mark.asyncio
async def test_attached_present_audit_event_bridged_to_parent_log(
    tmp_path: Path,
) -> None:
    """Tier 2: Half-B — the driver's ``presented`` P6 audit event is re-emitted onto the
    PARENT's EventLog with ``bridged_from=<driver_sid>`` provenance. RED on origin/main:
    ``presented`` lands only on the driver's own log (the #2570 bridge carried only
    ``pipeline_step_*``), so the parent audit trail is split."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log)
    caller = reg.get_or_load("worker")

    await run_pipeline_attached(
        reg,
        pipeline=_present_pipeline(),
        pipeline_name="shows",
        input=None,
        reply_to_agent="worker",
        reply_to_sid="main",
        state_log=state_log,
        tool="run_pipeline",
        caller_events=caller.router_host.events,
    )
    driver_sid = _driver_sid_from(caller.router_host.events)
    assert driver_sid is not None

    bridged = [
        e for e in caller.router_host.events.all()
        if e.type == "presented" and e.data.get("bridged_from")
    ]
    assert bridged, (
        "no bridged 'presented' event on the parent EventLog — the present audit trail "
        "is isolated in the driver-session's own log (the split-trail the bridge closes)."
    )
    (event,) = bridged  # exactly one bridged copy
    assert event.data["bridged_from"] == driver_sid
    assert event.data.get("pipeline_run_id"), "bridged present missing pipeline_run_id provenance"


@pytest.mark.asyncio
async def test_detached_present_not_bridged_to_caller(tmp_path: Path) -> None:
    """Tier 2: scope guard — a DETACHED (``start_pipeline_run``) pipeline's present is NOT
    bridged to the (non-attached) invoker: no ``pipeline_run_attached`` marker is emitted,
    so the caller's forwarder never bridge-subscribes → no bridged ``presented`` on the
    caller log and no presentation on the caller outbox. This pins the attached-only scope of
    P3.1's PARENT-bridge. (Detached present is now a DELIBERATE ``AuditOnlyNoSurface`` decision
    — audit-only, no orphan — landed in P3-item3; see
    ``test_spawn_routing_detached_fail_mode_2708``. It still, correctly, does not reach this
    non-attached caller.)"""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log)
    caller = reg.get_or_load("worker")

    rid = await start_pipeline_run(
        reg,
        pipeline=_present_pipeline(),
        pipeline_name="shows",
        input=None,
        reply_to_agent="worker",
        reply_to_sid="main",
        state_log=state_log,
    )
    run_dir = pipeline_run_dir(reyn_root(state_log.path), rid)

    # Drive the detached pump to terminal (result marker written).
    deadline = asyncio.get_event_loop().time() + 15.0
    while asyncio.get_event_loop().time() < deadline:
        if read_result(run_dir) is not None:
            break
        await asyncio.sleep(0.05)
    assert read_result(run_dir) is not None, "detached pipeline run did not reach terminal"

    bridged = [
        e for e in caller.router_host.events.all()
        if e.type == "presented" and e.data.get("bridged_from")
    ]
    assert bridged == [], "a detached present was unexpectedly bridged to the caller log"
    assert _presentations(_drain(caller.outbox)) == [], (
        "a detached present unexpectedly reached the caller outbox"
    )
