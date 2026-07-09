"""#2708 P3.2a — a chat-invoked pipeline's ``ask_user`` reaches the PARENT operator.

A pipeline run from within a chat session runs in a spawned DRIVER session, which gets
a fresh, listener-less ``InterventionRegistry(enforce_listener_presence=True)``. So a
``tool: ask_user`` step would hit the no-listener short-circuit
(``services/intervention_registry.py`` — ``dispatch`` returns ``InterventionAnswer(text="")``)
and **silently auto-refuse** — even under ``run_pipeline_attached`` where a live operator
is blocked on the parent (#2721, the intervention-delivery sibling of the #2707 present
gap fixed by P3.1).

P3.2a fixes this by construction: the attached driver-session is spawned with a
``SpawnBridgeInterventionListener`` bound to the PARENT (caller) session, so the driver's
router intervention bus dispatches on the PARENT's ``InterventionRegistry`` — which HAS
the live operator's listener. The operator is prompted on the parent surface and their
answer flows back to the driver's awaiting ``ask_user`` op (the future lives on the shared
``UserIntervention``). Reuses the P3.1 spawn-override seam (``intervention_bridge`` threaded
through ``spawn_session_recorded`` / ``spawn_session`` / ``_construct_session``).

Scope (P3-item3 completeness gate, NOT P3.2a): the DETACHED path (``start_pipeline_run``)
has no attached parent surface, so its ask_user keeps the fail-closed auto-refuse — a
tracked known-RED cell asserted here so it is explicit, not silently "correct".

Real ``AgentRegistry``/``Session``/``StateLog``/``PipelineExecutor``/intervention
machinery — no collaborator mocks. Asserts on the public outbox + intervention-registry
+ EventLog surfaces (the same operator-simulation pattern as the intervention e2e tests:
register a real listener, drive ``_maybe_answer_oldest_intervention``).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _async_wait import wait_until  # noqa: E402 — shared #1751 test wait helper

from reyn.core.events.state_log import StateLog  # noqa: E402
from reyn.core.pipeline.executor import Pipeline, ToolStep  # noqa: E402
from reyn.runtime.registry import AgentRegistry  # noqa: E402
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID, Session  # noqa: E402
from reyn.runtime.session_api import run_pipeline_attached, start_pipeline_run  # noqa: E402

_QUESTION = "REYN2708P32A which branch?"
_ANSWER = "REYN2708P32A-the-blue-branch"


def _agent_registry(tmp_path: Path, state_log: "StateLog") -> AgentRegistry:
    """Real AgentRegistry + Session factory that accepts BOTH spawn overrides (the
    widened factory protocol): the attached driver spawn threads a parent-bound
    ``intervention_bridge`` and the factory forwards it, so the driver's ask_user
    dispatches on the parent's live-operator registry by construction."""
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        return Session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            presentation_consumer=presentation_consumer,
            intervention_bridge=intervention_bridge,
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    if not reg.exists("worker"):
        reg.create("worker")
    return reg


def _ask_pipeline() -> Pipeline:
    return Pipeline(steps=[
        ToolStep(
            name="ask_user",
            args={"question": _QUESTION, "required": True},
            output="ans",
        ),
    ])


def _drain(queue: "asyncio.Queue") -> list:
    out: list = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def _interventions(msgs: list) -> list:
    return [m for m in msgs if getattr(m, "kind", None) == "intervention"]


def _driver_sid_from(events) -> "str | None":
    for e in events.all():
        if e.type == "pipeline_run_attached":
            return e.data.get("driver_sid")
    return None


@pytest.mark.asyncio
async def test_attached_ask_user_reaches_parent_operator_and_answer_flows_back(
    tmp_path: Path,
) -> None:
    """Tier 2: a sync-attached ``run_pipeline_attached`` ``ask_user`` step is delivered to
    the PARENT (caller) operator's intervention channel — the operator is prompted and
    their answer flows back into the driver's awaiting op. RED on origin/main: the driver's
    listener-less registry auto-refuses (``text=""``), so the operator is never prompted and
    the run proceeds on a fabricated empty answer."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log)
    caller = reg.get_or_load("worker")
    # The live operator: register a listener on the parent under the chat channel the
    # bridge routes to (the same id repl.py binds via bind_focus_listeners).
    caller.register_intervention_listener(DEFAULT_CHAT_CHANNEL_ID)

    async def _drive() -> dict:
        return await run_pipeline_attached(
            reg,
            pipeline=_ask_pipeline(),
            pipeline_name="asks",
            input=None,
            reply_to_agent="worker",
            reply_to_sid="main",
            state_log=state_log,
            tool="run_pipeline",
            caller_events=caller.router_host.events,
        )

    run_task = asyncio.ensure_future(_drive())
    driver = None
    try:
        # The bridged ask_user dispatches on the PARENT: its iv lands in the PARENT's
        # active-intervention queue (proof the ask reached the parent, not the driver's
        # own listener-less registry which would have auto-refused instantly).
        await wait_until(lambda: bool(caller.interventions.list_active()), timeout=15.0)
        # Capture the driver session while it is still live (the persistent driver is
        # removed from the registry once the attached run completes).
        driver_sid = _driver_sid_from(caller.router_host.events)
        assert driver_sid is not None
        driver = reg.get_session("worker", driver_sid)
        assert driver is not None
        # The operator was prompted on the parent surface (an "intervention" announce on
        # the parent's own outbox — the same queue+kind a chat-native ask_user reaches).
        assert _interventions(_drain(caller.outbox)), (
            "the operator was never prompted on the parent surface — the bridged ask_user "
            "did not announce on the parent's outbox."
        )
        # The operator answers on the parent (drives the same deliver path repl input does).
        consumed = await caller._maybe_answer_oldest_intervention(_ANSWER)
        assert consumed is True
        outcome = await asyncio.wait_for(run_task, timeout=15.0)
    finally:
        if not run_task.done():
            run_task.cancel()

    assert outcome["status"] == "ok"

    # The operator's answer was DELIVERED for the bridged iv — its resolution fired
    # ``user_answered_intervention`` on the PARENT's log (dispatch ran on the parent), with
    # the real answer text, not the empty auto-refuse.
    answered = [
        e for e in caller.router_host.events.all()
        if e.type == "user_answered_intervention"
    ]
    assert any(e.data.get("answer_text") == _ANSWER for e in answered), (
        "the operator's answer was not delivered for the bridged ask_user on the parent log."
    )
    # And it flowed back INTO the driver's ask_user op (its P6 ``user_intervention_received``
    # carries the real answer, not the fabricated empty auto-refuse text).
    received = [
        e for e in driver.router_host.events.all()
        if e.type == "user_intervention_received"
    ]
    assert received, "no user_intervention_received on the driver log (ask never resolved)"
    assert any(e.data.get("answer") == _ANSWER for e in received), (
        "the driver's ask_user did not receive the operator's answer — it resolved on a "
        "fabricated/empty answer (the silent auto-refuse this bridge fixes)."
    )


@pytest.mark.asyncio
async def test_attached_ask_user_not_stalled_uses_live_parent_listener(
    tmp_path: Path,
) -> None:
    """Tier 2: the bridged iv is routed to the parent's LIVE listener, not parked in the
    parent's stalled queue — the bridge stamps the parent's registered channel id, so the
    origin-pin routing (``InterventionCoordinator``) delivers rather than stalls. Guards
    that the bridge picks a channel the parent actually listens on."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log)
    caller = reg.get_or_load("worker")
    caller.register_intervention_listener(DEFAULT_CHAT_CHANNEL_ID)

    async def _drive() -> dict:
        return await run_pipeline_attached(
            reg,
            pipeline=_ask_pipeline(),
            pipeline_name="asks",
            input=None,
            reply_to_agent="worker",
            reply_to_sid="main",
            state_log=state_log,
            tool="run_pipeline",
            caller_events=caller.router_host.events,
        )

    run_task = asyncio.ensure_future(_drive())
    try:
        await wait_until(lambda: bool(caller.interventions.list_active()), timeout=15.0)
        # Delivered (active), NOT stalled — the parent's live listener owns it.
        assert caller.list_stalled_interventions() == [], (
            "the bridged ask_user was parked in the parent's stalled queue instead of "
            "reaching its live listener — the bridge stamped a channel the parent does "
            "not listen on."
        )
        delivered = await caller._maybe_answer_oldest_intervention(_ANSWER)
        assert delivered is True
        outcome = await asyncio.wait_for(run_task, timeout=15.0)
    finally:
        if not run_task.done():
            run_task.cancel()
    assert outcome["status"] == "ok"


@pytest.mark.asyncio
async def test_detached_ask_user_does_not_reach_invoker_known_red(tmp_path: Path) -> None:
    """Tier 2: scope guard (known-RED cell) — a DETACHED (``start_pipeline_run``) pipeline's
    ``ask_user`` has NO attached parent surface, so the driver keeps its self-bound,
    listener-less intervention registry: the ask does NOT reach the (non-attached) invoker's
    live operator listener (over a bounded window the invoker is never prompted). This pins
    the attached-only scope of P3.2a — detached/async intervention is the tracked P3-item3
    completeness-gate cell, NOT addressed here and NOT silently blessed as correct (the
    detached ask does not resolve against the invoker; whether it fail-closes or stalls in
    the driver's own registry is the known-RED behavior tracked for the completeness gate)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log)
    caller = reg.get_or_load("worker")
    caller.register_intervention_listener(DEFAULT_CHAT_CHANNEL_ID)

    try:
        await start_pipeline_run(
            reg,
            pipeline=_ask_pipeline(),
            pipeline_name="asks",
            input=None,
            reply_to_agent="worker",
            reply_to_sid="main",
            state_log=state_log,
        )
        # Over a bounded window (the detached driver spawns + pumps + reaches its ask_user),
        # the invoker's live operator listener is NEVER consulted — no intervention parks on
        # the caller's active queue. (An attached bridge WOULD park one there; see the
        # attached test. This is the not-bridged scope.)
        deadline = asyncio.get_event_loop().time() + 4.0
        while asyncio.get_event_loop().time() < deadline:
            assert caller.interventions.list_active() == [], (
                "a detached ask_user unexpectedly reached the invoker's live listener — "
                "detached is out of P3.2a scope (known-RED)."
            )
            await asyncio.sleep(0.1)
    finally:
        # Teardown: the detached driver's ask_user may still be pending in its own registry
        # (known-RED), so cancel the registry's live session run tasks to avoid a lingering
        # coroutine at loop close.
        for task in list(reg._tasks.values()):
            if not task.done():
                task.cancel()
