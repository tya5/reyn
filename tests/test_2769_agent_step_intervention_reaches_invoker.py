"""#2769 — an agent-step's intervention (+ present) reaches the pipeline INVOKER when attached.

Owner refinement of P3-item3 (#2706/#2710): ``run_agent_step`` used to hardcode
``AuditOnlyNoSurface`` for its ephemeral leaf worker, so an agent-step launched inside a
pipeline attached to a live chat ALWAYS refused ask_user / permission / present. #2769 threads
the invoking pipeline's live driver-session down to ``run_agent_step`` and swaps the routing to
``BridgeToParent(invoker_session)`` when attached (``AuditOnlyNoSurface`` when detached / headless).
So an agent-step's ``ask_user`` / permission JIT approval / ``safety.limit`` / elicitation AND its
``present`` reach the ORIGINATING operator via the #2735 compositional transitive bridge
(agent-step → driver → root operator), while a detached run stays fail-closed.

Real ``AgentRegistry`` / ``Session`` / ``RouterLoop`` / ``MessageBus`` / intervention +
present machinery — no collaborator mocks. The ONLY faked collaborator is the leaf worker's
LLM completion (a fixed plain-text reply via the ``_loop_observer`` seam), incidental to the
routing claim under test (the spawn's DECLARED routing + its reachability). Asserts on public
surfaces: the worker's routing accessors, the operator's active-intervention queue + outbox,
the typed permission decision (``PermissionError`` = DENY). Follows the exact operator-reach
pattern of ``test_spawn_routing_detached_fail_mode_2708`` /
``test_2708_p32a_spawn_bridge_intervention``.

Safety crux (#2769): the DETACHED permission fail-closed DENY is a consumer-side deny-by-default
property, INDEPENDENT of the routing swap — an AuditOnly refusal (``choice_id=None``) falls
through to DENY at every permission consumer. ``test_detached_agent_step_permission_fail_closed_deny``
pins it: a detached agent-step worker's own AuditOnly bus, fed to the real permission consumer,
yields DENY (``PermissionError``), never allow.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.present import handle as present_handle
from reyn.data.workspace.workspace import Workspace
from reyn.intervention_choices import YES, generic_yn_choices
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.presentation_consumer import (
    AuditOnlyPresentationConsumer,
    SpawnBridgePresentationConsumer,
)
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID, Session
from reyn.runtime.session_api import run_agent_step
from reyn.runtime.session_buses import (
    AuditOnlyInterventionBridge,
    SpawnBridgeInterventionListener,
)
from reyn.schemas.models import PresentIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import UserIntervention


class _ScriptedReply:
    """A fixed plain-text LLM turn (no tool_calls) — the leaf worker's turn completes with
    this content, so ``run_agent_step`` returns and its ephemeral worker vanishes; the routing
    it was SPAWNED with (the #2769 claim) is captured by the recording factory before then. NOT
    a MagicMock: a signature drift in ``call_llm_tools`` raises TypeError here as in production."""

    def __init__(self, content: str = "leaf worker done") -> None:
        self.content = content

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        return LLMToolCallResult(
            content=self.content, tool_calls=[], finish_reason="stop", usage=TokenUsage(),
        )


def _recording_registry(tmp_path: Path, state_log: "StateLog") -> "tuple[AgentRegistry, list]":
    """Real AgentRegistry whose factory RECORDS every Session it builds (so a spawned worker's
    public routing is inspectable after it vanishes) and wires a scripted plain-text LLM onto each
    session's real RouterLoopDriver via ``_loop_observer`` (post-construction observer, not a
    factory-seam bypass)."""
    built: list[Session] = []
    holder: dict = {}
    scripted = _ScriptedReply()

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        s = Session(
            agent_name=profile.name, state_log=state_log, registry=holder.get("reg"),
            non_interactive=True, presentation_consumer=presentation_consumer,
            intervention_bridge=intervention_bridge,
        )
        s._loop_driver._loop_observer = (  # noqa: SLF001 — designed Tier-2 LLM seam
            lambda loop: setattr(loop, "_llm_caller", scripted)
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


def _cancel_tasks(reg: "AgentRegistry") -> None:
    for task in list(reg._tasks.values()):  # noqa: SLF001 — teardown precedent (sibling tests)
        if not task.done():
            task.cancel()


# ── Attached: agent-step ask_user reaches the invoker operator ───────────────────────────


@pytest.mark.asyncio
async def test_attached_agent_step_ask_user_reaches_invoker_operator(tmp_path: Path) -> None:
    """Tier 2: an agent-step run with an ``invoker_session`` (attached pipeline) spawns its leaf
    worker ``BridgeToParent(invoker)`` — the worker's ``ask_user`` reaches the INVOKER operator's
    live listener and the operator's answer flows back. RED on main: ``run_agent_step`` hardcoded
    ``AuditOnlyNoSurface``, so the worker's bridge refused locally and NEVER reached the invoker."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg, built = _recording_registry(tmp_path, state_log)
    invoker = reg.get_or_load("worker")
    # The live operator on the invoking (pipeline-originator) session.
    invoker.register_intervention_listener(DEFAULT_CHAT_CHANNEL_ID)

    await run_agent_step(reg, identity="worker", prompt="do a thing", invoker_session=invoker)

    # The worker was spawned BridgeToParent(invoker) — the #2769 routing swap (RED on main: it
    # would be an AuditOnlyInterventionBridge that refuses locally, never reaching the invoker).
    workers = [s for s in built if isinstance(s.intervention_bridge, SpawnBridgeInterventionListener)]
    assert workers, (
        "the agent-step worker was NOT routed BridgeToParent(invoker) — it kept the pre-#2769 "
        "AuditOnly bridge and its ask_user would refuse locally, never reaching the operator."
    )
    worker = workers[-1]
    assert worker.intervention_bridge.parent_session is invoker

    # Operator-reach proof: the worker's ask_user (via its DECLARED bridge — the exact bus its op
    # builds) lands on the INVOKER operator's active queue and resolves with the operator's answer.
    bus = worker.intervention_bridge.bus(run_id="r", actor="agent-step")
    iv = UserIntervention(kind="ask_user", prompt="which branch?")
    deliver = asyncio.ensure_future(bus.deliver(iv))
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 5.0
    while loop.time() < deadline and not invoker.interventions.list_active():
        await asyncio.sleep(0.02)
    assert invoker.interventions.list_active(), (
        "the agent-step ask_user never reached the invoker operator's active queue (it refused "
        "locally instead of bridging to the originator)."
    )
    # The operator was prompted on the invoker's own surface (chat-native intervention announce).
    assert _by_kind(_drain(invoker.outbox), "intervention"), (
        "the operator was never prompted on the invoker surface — the bridged ask_user did not "
        "announce on the invoker's outbox."
    )
    consumed = await invoker._maybe_answer_oldest_intervention("blue")  # noqa: SLF001 — operator sim
    assert consumed is True
    answer = await asyncio.wait_for(deliver, timeout=5.0)  # resolves via the operator, no hang
    assert (answer.choice_id or answer.text) == "blue"
    assert answer.refused is False

    _cancel_tasks(reg)


# ── Attached: agent-step permission (bus-wide) reaches the invoker operator ───────────────


@pytest.mark.asyncio
async def test_attached_agent_step_permission_reaches_invoker_operator(tmp_path: Path) -> None:
    """Tier 2: bus-wide — the routing swap governs the WHOLE intervention bus, so an agent-step
    ``permission.generic`` JIT-approval (the kind the owner specifically wants the operator to see)
    reaches the invoker operator via the same bridge, not just ``ask_user``. Same DECLARED-bridge
    reachability proof, with a permission intervention."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg, built = _recording_registry(tmp_path, state_log)
    invoker = reg.get_or_load("worker")
    invoker.register_intervention_listener(DEFAULT_CHAT_CHANNEL_ID)

    await run_agent_step(reg, identity="worker", prompt="do a thing", invoker_session=invoker)

    workers = [s for s in built if isinstance(s.intervention_bridge, SpawnBridgeInterventionListener)]
    assert workers, "the agent-step worker was not routed BridgeToParent(invoker) (#2769)."
    worker = workers[-1]

    bus = worker.intervention_bridge.bus(run_id="r", actor="agent-step")
    iv = UserIntervention(
        kind="permission.generic", prompt="Allow tool 'shell'?", choices=generic_yn_choices(),
    )
    deliver = asyncio.ensure_future(bus.deliver(iv))
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 5.0
    while loop.time() < deadline and not invoker.interventions.list_active():
        await asyncio.sleep(0.02)
    assert invoker.interventions.list_active(), (
        "the agent-step PERMISSION prompt never reached the invoker operator — the routing swap "
        "must be bus-wide (permission.* / safety.limit / elicitation), not ask_user-only."
    )
    # The operator picks the affirmative choice on the invoker surface (the authoritative
    # closed-set choice path the inline selector uses — bypasses text/hotkey match_choice).
    consumed = await invoker.answer_oldest_intervention_choice(YES)
    assert consumed is True
    answer = await asyncio.wait_for(deliver, timeout=5.0)
    # The operator's affirmative reached the agent-step's permission op (a real grant, not a refusal).
    assert answer.choice_id == YES
    assert answer.refused is False

    _cancel_tasks(reg)


# ── Attached: agent-step present reaches the invoker outbox (routing, not filter) ─────────


@pytest.mark.asyncio
async def test_attached_agent_step_present_reaches_invoker_outbox(tmp_path: Path) -> None:
    """Tier 2: present symmetry falls out of the ROUTING decision (NOT the ``kind=='agent'`` reply
    filter). The attached worker is spawned ``SpawnBridgePresentationConsumer`` bound to the invoker,
    so a ``present`` renders onto the INVOKER's outbox (reaching the operator) — RED on main: the
    worker was AuditOnly, present was a no-op sink that reached no one."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg, built = _recording_registry(tmp_path, state_log)
    invoker = reg.get_or_load("worker")

    await run_agent_step(reg, identity="worker", prompt="do a thing", invoker_session=invoker)

    workers = [
        s for s in built if isinstance(s.presentation_consumer, SpawnBridgePresentationConsumer)
    ]
    assert workers, (
        "the agent-step worker was NOT routed SpawnBridgePresentationConsumer(invoker) — its "
        "present would hit the pre-#2769 AuditOnly no-op sink, reaching no operator surface."
    )
    worker = workers[-1]

    # Drive a present op through the worker's DECLARED present sink (the exact renderer its op
    # builds): SpawnBridge binds to the invoker, so the render lands on the INVOKER's outbox.
    events = EventLog()
    sink = worker.presentation_consumer.sink(worker)
    ctx = OpContext(
        workspace=Workspace(events=events), events=events, permission_decl=PermissionDecl(),
        presentation_renderer=sink,
    )
    op = PresentIROp(
        kind="present", data_inline={"v": "AGENTSTEP2769"},
        blueprint={"component": "text", "text": {"$bind": "/v"}},
    )
    result = await present_handle(op, ctx)
    assert result["ok"] is True
    presented = _by_kind(_drain(invoker.outbox), "presentation")
    assert presented, (
        "the agent-step present did NOT land on the invoker's outbox — present symmetry must ride "
        "the routing (BridgeToParent's SpawnBridgePresentationConsumer), reaching the operator."
    )

    _cancel_tasks(reg)


# ── Detached: agent-step ask_user refuses (unchanged) ────────────────────────────────────


@pytest.mark.asyncio
async def test_detached_agent_step_ask_user_refuses(tmp_path: Path) -> None:
    """Tier 2: a DETACHED agent-step (no ``invoker_session`` — a headless ``reyn pipe`` run or a
    direct executor call) keeps the reviewed ``AuditOnlyNoSurface`` routing: its ``ask_user``
    resolves to a typed refusal, never reaching (nor hanging on) any operator."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg, built = _recording_registry(tmp_path, state_log)

    await run_agent_step(reg, identity="worker", prompt="do a thing")  # no invoker_session

    workers = [s for s in built if isinstance(s.intervention_bridge, AuditOnlyInterventionBridge)]
    assert workers, (
        "a detached agent-step worker was not routed AuditOnly — the fail-closed detached routing "
        "must be preserved when no invoker_session is threaded in."
    )
    worker = workers[-1]
    bus = worker.intervention_bridge.bus(run_id="r", actor="agent-step")
    iv = UserIntervention(kind="ask_user", prompt="which branch?")
    answer = await asyncio.wait_for(bus.deliver(iv), timeout=3.0)  # MUST NOT hang
    assert answer.refused is True

    _cancel_tasks(reg)


# ── Detached: agent-step permission → fail-closed DENY (the safety crux) ──────────────────


@pytest.mark.asyncio
async def test_detached_agent_step_permission_fail_closed_deny(tmp_path: Path) -> None:
    """Tier 2: (safety crux) a DETACHED agent-step's AuditOnly permission refusal is interpreted as
    DENY (fail-closed) by the real permission consumer — NOT allow. The detached worker's OWN
    AuditOnly bus, fed to a real ``PermissionResolver.require_tool`` (the consumer an agent-step's
    permission op reaches), yields a ``PermissionError`` (deny). This is the security-critical
    invariant: the routing change cannot open a refuse→allow hole because DENY is a consumer-side
    deny-by-default property (``choice_id=None`` falls through to deny), independent of routing."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg, built = _recording_registry(tmp_path, state_log)

    await run_agent_step(reg, identity="worker", prompt="do a thing")  # detached

    workers = [s for s in built if isinstance(s.intervention_bridge, AuditOnlyInterventionBridge)]
    assert workers, "the detached agent-step worker was not routed AuditOnly (#2769 fail-closed)."
    worker = workers[-1]

    # The EXACT bus the worker's permission op would dispatch on (its declared AuditOnly bridge).
    bus = worker.intervention_bridge.bus(run_id="r", actor="agent-step")
    # A real permission consumer: the tool IS declared (passes the static-authority gate) and is
    # NOT pre-approved, so it reaches the interactive prompt → the AuditOnly bus → a refusal.
    resolver = PermissionResolver({}, project_root=tmp_path, interactive=True)
    decl = PermissionDecl(tool=["risky_tool"])

    with pytest.raises(PermissionError):
        # The AuditOnly refusal (choice_id=None) is consumed as DENY — require_tool raises. A
        # refuse→allow hole would let this pass (the serious security regression this pins against).
        await resolver.require_tool(decl, "risky_tool", bus)

    _cancel_tasks(reg)
