"""#2708 P3-item3 — the DELIBERATE detached/headless fail-mode (no orphan present, no ask hang).

The spawn-axis completeness gate turns the pre-fix INCIDENTAL detached fail-modes into a
reviewed, deliberate ``AuditOnlyNoSurface`` decision:

  * detached ``present`` (#2710) — no longer orphans a ``"presentation"`` message on the driver's
    own undrained outbox; the render is audit-only (the durable ``presented`` P6 event fires).
  * detached ``ask_user`` — no longer HANGS. Step-1 primary-data resolution: the self-bound
    ``ChatInterventionBus`` stamps ``origin_channel_id="tui"`` and
    ``InterventionCoordinator.dispatch`` parks it stalled + ``await iv.future`` forever (verified:
    a detached ask_user pipeline never reached terminal in >6s). ``AuditOnlyInterventionBridge``
    resolves it IMMEDIATELY with a typed, reason'd refusal — terminal in well under a second.
  * agent-step ``present`` (#2706) — the ephemeral leaf worker is spawned AuditOnly, so its
    present is audit-only, not silently self-bound then dropped by the ``kind=="agent"`` filter.

Real ``AgentRegistry`` / ``Session`` / ``StateLog`` / ``PipelineExecutor`` / present + ask_user
ops + intervention machinery — no collaborator mocks. Asserts on public surfaces (outbox,
EventLog, the typed ``InterventionAnswer`` / ask_user ack, the session's public routing
accessors), never Rich formatting.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.config_recovery import reyn_root
from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import Pipeline, ToolStep
from reyn.core.pipeline.work_order import pipeline_run_dir, read_result
from reyn.runtime.presentation_consumer import (
    AuditOnlyPresentationConsumer,
    AuditOnlyPresentationSink,
)
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_api import run_agent_step, start_pipeline_run
from reyn.runtime.session_buses import (
    NO_SURFACE_REFUSAL_REASON,
    AuditOnlyInterventionBridge,
)


def _recording_registry(tmp_path: Path, state_log: "StateLog") -> "tuple[AgentRegistry, list]":
    """Real AgentRegistry whose factory RECORDS every Session it builds — so a test can inspect a
    spawned driver/worker session's public routing even after the ephemeral session vanishes from
    the registry map (the Python ref + its in-memory EventLog survive)."""
    built: list[Session] = []
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        s = Session(
            agent_name=profile.name, state_log=state_log, registry=holder.get("reg"),
            non_interactive=True, presentation_consumer=presentation_consumer,
            intervention_bridge=intervention_bridge,
        )
        built.append(s)
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    if not reg.exists("worker"):
        reg.create("worker")
    return reg, built


def _drain(queue: "asyncio.Queue") -> list:
    out: list = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def _by_kind(msgs: list, kind: str) -> list:
    return [m for m in msgs if getattr(m, "kind", None) == kind]


# ── Unit: the AuditOnly routing primitives ──────────────────────────────────────────────


def test_audit_only_presentation_consumer_sink_is_noop() -> None:
    """Tier 2: AuditOnlyPresentationConsumer yields an AuditOnlyPresentationSink — a no-op visible
    draw (the durable audit trail is the upstream ``presented`` event), never an orphan outbox sink."""
    sink = AuditOnlyPresentationConsumer().sink(object())
    assert isinstance(sink, AuditOnlyPresentationSink)
    # The sink's surface is the REGISTERED "null" no-op neutralizer — robust across all present
    # blueprint shapes (an "none"-surface sink fails the fail-closed guard for a text-leaf blueprint).
    assert sink.surface_name == "null"
    sink.render(object())  # no-op, must not raise (fire-and-continue)


@pytest.mark.asyncio
async def test_present_op_via_audit_only_sink_fires_audit_event_and_no_orphan() -> None:
    """Tier 1: present-op contract through the AuditOnly sink — the ``presented`` P6 audit event
    STILL fires (the render is durable / replay-visible, not lost) and the visible draw is a
    documented no-op (no orphan outbox message). This is the audit-only-not-lost guarantee the
    detached/agent-step routing relies on, pinned at the op layer (deterministic, no LLM)."""
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.context import OpContext
    from reyn.core.op_runtime.present import handle
    from reyn.data.workspace.workspace import Workspace
    from reyn.schemas.models import PresentIROp
    from reyn.security.permissions.permissions import PermissionDecl

    events = EventLog()
    sink = AuditOnlyPresentationConsumer().sink(object())
    ctx = OpContext(
        workspace=Workspace(events=events), events=events, permission_decl=PermissionDecl(),
        presentation_renderer=sink,
    )
    op = PresentIROp(
        kind="present", data_inline={"v": "AUDITONLYMARK"},
        blueprint={"component": "text", "text": {"$bind": "/v"}},
    )
    result = await handle(op, ctx)
    assert result["ok"] is True  # present succeeded (not errored/lost)
    presented = [e for e in events.all() if e.type == "presented"]
    assert presented, "the AuditOnly present did NOT emit a 'presented' audit event — trail lost"
    # The sink's own surface is the null/no-op surface — the visible draw reached no orphan queue.
    assert presented[0].data.get("surface") == [sink.surface_name]


@pytest.mark.asyncio
async def test_audit_only_intervention_bridge_refuses_with_reason_immediately() -> None:
    """Tier 2: AuditOnlyInterventionBridge.bus().deliver returns a TYPED, reason'd refusal
    IMMEDIATELY (refused=True) — never enqueues/awaits (no park/hang), never a silent empty."""
    from reyn.user_intervention import UserIntervention

    bus = AuditOnlyInterventionBridge().bus(run_id="r", actor="a")
    iv = UserIntervention(kind="ask_user", prompt="which?")
    answer = await asyncio.wait_for(bus.deliver(iv), timeout=2.0)
    assert answer.refused is True
    assert answer.reason == NO_SURFACE_REFUSAL_REASON
    assert answer.text == ""


@pytest.mark.asyncio
async def test_ask_user_op_via_audit_only_bridge_returns_typed_refusal() -> None:
    """Tier 1: ask_user-op contract through the AuditOnly bridge — the op returns a TYPED refusal
    (``status="refused"`` carrying the reason), NOT a fabricated empty ``status="ok"`` answer, and
    emits a ``user_intervention_received`` event with ``refused=True``. Deterministic (no LLM), and
    completes immediately (no park/hang)."""
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.ask_user import handle
    from reyn.core.op_runtime.context import OpContext
    from reyn.data.workspace.workspace import Workspace
    from reyn.schemas.models import AskUserIROp
    from reyn.security.permissions.permissions import PermissionDecl

    events = EventLog()
    ctx = OpContext(
        workspace=Workspace(events=events), events=events, permission_decl=PermissionDecl(),
        intervention_bus=AuditOnlyInterventionBridge().bus(),
    )
    op = AskUserIROp(kind="ask_user", question="which branch?", required=True)
    result = await asyncio.wait_for(handle(op, ctx), timeout=2.0)
    assert result["status"] == "refused", "ask_user did not surface the typed refusal"
    assert result["reason"] == NO_SURFACE_REFUSAL_REASON
    assert result["answer"] == ""  # never a fabricated non-empty answer
    received = [e for e in events.all() if e.type == "user_intervention_received"]
    assert any(e.data.get("refused") is True for e in received), (
        "the refusal was not recorded as refused=True — it looked like a normal empty answer"
    )


# ── #2710: detached present is audit-only (no orphan) ────────────────────────────────────


@pytest.mark.asyncio
async def test_detached_present_is_audit_only_no_orphan_outbox(tmp_path: Path) -> None:
    """Tier 2: a DETACHED pipeline's ``present`` no longer orphans a ``"presentation"`` message on
    the driver's own undrained outbox (the pre-fix #2710 silent-loss); it is audit-only — the
    driver's ``presented`` P6 event still fires (audit trail preserved), zero outbox presentation."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg, built = _recording_registry(tmp_path, state_log)

    rid = await start_pipeline_run(
        reg, pipeline=Pipeline(steps=[
            ToolStep(name="present", args={"data_inline": {"label": "AUDITONLYMARK"}}, output="a"),
        ]),
        pipeline_name="shows", input=None,
        reply_to_agent="worker", reply_to_sid="main", state_log=state_log,
    )
    run_dir = pipeline_run_dir(reyn_root(state_log.path), rid)
    deadline = asyncio.get_event_loop().time() + 10.0
    while asyncio.get_event_loop().time() < deadline:
        if read_result(run_dir) is not None:
            break
        await asyncio.sleep(0.05)
    assert read_result(run_dir) is not None, "detached present pipeline did not reach terminal"

    # The detached driver was spawned AuditOnly (its present sink is the no-op) — identify it as
    # the built session carrying that routing decision. RED if the detached spawn self-bound
    # instead (#2710 orphan class): no AuditOnly-routed session would exist.
    audit_drivers = [
        s for s in built if isinstance(s.presentation_consumer, AuditOnlyPresentationConsumer)
    ]
    assert audit_drivers, (
        "no AuditOnly-routed driver was built — the detached spawn did not route AuditOnly "
        "(its present would self-bind to an undrained outbox, the #2710 orphan class)."
    )
    driver = audit_drivers[-1]
    assert read_result(run_dir).get("status") == "ok", "detached present pipeline did not succeed"
    assert _by_kind(_drain(driver.outbox), "presentation") == [], (
        "the detached driver orphaned a presentation on its own outbox (the #2710 silent-loss) — "
        "AuditOnly makes present a no-op visible draw (the audit trail is the upstream "
        "'presented' event; see test_present_op_via_audit_only_sink_fires_audit_event_and_no_orphan)."
    )

    for task in list(reg._tasks.values()):
        if not task.done():
            task.cancel()


# ── detached ask_user: deliberate typed refusal, NO hang ─────────────────────────────────


@pytest.mark.asyncio
async def test_detached_ask_user_refuses_deliberately_no_hang(tmp_path: Path) -> None:
    """Tier 2: step-1 resolution pinned. A DETACHED pipeline's ``ask_user`` now RESOLVES (terminal
    reached quickly — NOT the pre-fix origin-pin park/hang that never reached terminal in >6s) via
    a DELIBERATE typed refusal: the driver's ``user_intervention_received`` event carries
    ``refused=True`` + the reason, not a fabricated empty auto-refuse."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg, built = _recording_registry(tmp_path, state_log)

    rid = await start_pipeline_run(
        reg, pipeline=Pipeline(steps=[
            ToolStep(name="ask_user", args={"question": "which branch?", "required": True}, output="a"),
        ]),
        pipeline_name="asks", input=None,
        reply_to_agent="worker", reply_to_sid="main", state_log=state_log,
    )
    run_dir = pipeline_run_dir(reyn_root(state_log.path), rid)

    # NO HANG: terminal within a tight window (the pre-fix hang never reached it in >6s).
    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        if read_result(run_dir) is not None:
            break
        await asyncio.sleep(0.02)
    assert read_result(run_dir) is not None, (
        "detached ask_user did NOT reach terminal within 5s — the origin-pin park/hang was not "
        "closed by the AuditOnly refusal."
    )

    # The driver was spawned AuditOnly (the routing that turns the pre-fix hang into a deliberate
    # refusal) — identify it as the built session carrying an AuditOnly intervention bridge. The
    # refusal SEMANTICS (status="refused" + reason, not silent-empty) are pinned deterministically
    # at the op layer in test_ask_user_op_via_audit_only_bridge_returns_typed_refusal.
    audit_drivers = [
        s for s in built if isinstance(s.intervention_bridge, AuditOnlyInterventionBridge)
    ]
    assert audit_drivers, (
        "no AuditOnly-routed driver was built — the detached ask_user would origin-pin-park/hang."
    )

    for task in list(reg._tasks.values()):
        if not task.done():
            task.cancel()


# ── #2706: agent-step worker is spawned AuditOnly ────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_step_worker_spawned_audit_only(tmp_path: Path) -> None:
    """Tier 2: #2706 root-cause-i — ``run_agent_step`` spawns its ephemeral leaf worker with an
    AuditOnly routing (present audit-only, ask_user typed-refusal), NOT the pre-fix self-bound
    consumer that orphaned a present onto the worker's own outbox for the ``kind=="agent"`` filter
    to silently drop. Pinned on the worker's PUBLIC routing accessors."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg, built = _recording_registry(tmp_path, state_log)

    # The worker's one router turn has no live LLM here (returns empty agent text); the spawn +
    # its declared routing is what #2706 root-cause-i fixes — captured via the recording factory.
    try:
        await asyncio.wait_for(
            run_agent_step(reg, identity="worker", prompt="do a thing", timeout=5.0),
            timeout=20.0,
        )
    except Exception:
        # A router/LLM failure in the worker turn is irrelevant to this test's claim (the spawn's
        # DECLARED routing) — the worker Session was already built + recorded before the turn ran.
        pass

    # The ephemeral worker was spawned AuditOnly (#2706 root-cause-i) — identify it as the built
    # session carrying that routing. RED if run_agent_step self-bound the worker instead (its
    # present would orphan on an undrained outbox and be dropped by the kind=='agent' filter).
    audit_workers = [
        s for s in built
        if isinstance(s.presentation_consumer, AuditOnlyPresentationConsumer)
        and isinstance(s.intervention_bridge, AuditOnlyInterventionBridge)
    ]
    assert audit_workers, (
        "run_agent_step did not spawn its ephemeral worker AuditOnly (#2706 root-cause-i) — its "
        "present would self-bind to an undrained outbox, dropped by the kind=='agent' filter."
    )

    for task in list(reg._tasks.values()):
        if not task.done():
            task.cancel()
