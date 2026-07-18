"""#2707 — a chat-invoked pipeline's ``tool: present`` step reaches the PARENT chat surface (part of the #2688 sweep).

#2692 opened the pipeline INVOCATION surface for ``present`` and #2702 wired the
headless ``reyn pipe run`` RENDER surface, but the sync-attached driver-session
path (``run_pipeline`` / ``run_pipeline_inline`` invoked from WITHIN a chat) left
present's render isolated: a ``present`` step runs inside the spawned
driver-session and renders through the DRIVER's own ``OutboxPresentationRenderer``
onto the DRIVER session's outbox. ``MessageBus.request`` (the attached pump)
drains that outbox for quiescence, but the sync result contract only reads the
terminal ``read_result`` marker — so the drained ``"presentation"`` message was
silently discarded and the parent chat surface (the caller session's outbox, the
one the REPL/CUI actually drains + renders) never saw it. present returned
``ok:True`` regardless (a silent purpose-failure — the owner hit this directly).

This proves the reachable-FOR-PURPOSE bar at the parent surface: drive a real
sync-attached ``run_pipeline_attached`` of a real ``tool: present`` step and
assert the presented marker actually lands on the CALLER session's outbox as a
``"presentation"`` message — the SAME queue+kind a chat-native present reaches —
NOT merely on the driver-session's isolated outbox. Real
``AgentRegistry``/``Session``/``StateLog``/``PipelineExecutor``/present op — no
collaborator mocks; asserts the presented CONTENT (a marker substring) on the
public outbox surface, never exact Rich formatting/whitespace (a Tier-4 pin).

# #2708 P3.1 UPDATE — the #2707 interim outbox-forward is now REMOVED; the driver's
present reaches the caller by CONSTRUCTION (the driver spawns with a parent-bound
``SpawnBridgePresentationConsumer`` — its present sink IS the caller's sink). This test
stays GREEN via that new mechanism and, because it asserts EXACTLY ONE ``"presentation"``
on the caller outbox, now doubles as the single-delivery guard: the forward + the bridge
together would double-deliver, so a resurrected forward makes the ``(presented,) = ...``
unpack fail. The mechanism changed (forward → inherited sink); the reachable-for-purpose
outcome asserted here did not.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import Pipeline, ToolStep
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_api import run_pipeline_attached
from reyn.runtime.session_params import PresentationWiring

# A distinctive token carried in the PRESENTED data. Its purpose is to prove the
# render reached the PARENT surface — it is NOT part of the present op's compact
# ``ok:True`` ack, so its presence on the caller outbox is the forward, not an ack echo.
_MARKER = "REYN2707PRESENTMARKER"


class _ScriptedAgentReply:
    """One fixed plain-text turn — the LLM is incidental to what's under test."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.calls += 1
        return LLMToolCallResult(
            content=self.content, tool_calls=[], finish_reason="stop", usage=TokenUsage(),
        )


def _agent_registry(tmp_path: Path, state_log: "StateLog") -> AgentRegistry:
    """Real AgentRegistry + real Session factory (same discipline as the IS-6
    attached test): every Session is born with the production
    ``presentation_renderer_factory`` (→ ``OutboxPresentationRenderer`` onto its
    own outbox), so a driver-session present renders exactly as in production."""
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        # #2708 P3.1: the attached driver spawn threads a parent-bound
        # SpawnBridgePresentationConsumer through the factory; accept + forward it so the
        # driver's present renders to the PARENT's outbox by construction (None on the
        # non-driver caller session = Session's default self-bound outbox consumer).
        return Session(
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
    """A one-step pipeline whose ``tool: present`` step shows inline data (marker
    inside) via the stage-3 default viewer — no view authoring needed."""
    return Pipeline(steps=[
        ToolStep(name="present", args={"data_inline": {"label": _MARKER}}, output="ack"),
    ])


def _drain(queue: "asyncio.Queue") -> list:
    out: list = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


@pytest.mark.asyncio
async def test_attached_pipeline_present_reaches_parent_caller_outbox(
    tmp_path: Path,
) -> None:
    """Tier 2: a sync-attached ``run_pipeline_attached`` of a ``tool: present``
    step lands the driver-session's ``"presentation"`` render on the CALLER
    session's own outbox (the parent chat surface the REPL/CUI drains), carrying
    the presented marker — EXACTLY ONCE. #2708 P3.1: this now holds by construction
    (the driver inherits the parent's present sink via ``SpawnBridgePresentationConsumer``),
    not by the removed #2707 drain-and-copy forward. The single ``"presentation"``
    (``(presented,) = ...`` unpack) is the single-delivery guard: bridge + a resurrected
    forward would double-deliver."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log)
    caller = reg.get_or_load("worker")  # (worker, main) = the reply/parent-chat address

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

    # The parent chat surface = the CALLER session's own outbox (what the REPL/CUI
    # drains + renders), NOT the driver-session's isolated outbox.
    caller_msgs = _drain(caller.outbox)
    presentations = [m for m in caller_msgs if getattr(m, "kind", None) == "presentation"]
    assert presentations, (
        "no 'presentation' message reached the caller's outbox — a chat-invoked "
        "pipeline's present render is isolated in the driver-session's outbox "
        "(the parent chat surface never sees it)."
    )
    # The presented marker actually rode the forwarded render (proves it is the
    # render's data, not the compact ok:True ack).
    (presented,) = presentations  # exactly one — unpack fails RED if 0 or >1
    assert _MARKER in json.dumps(presented.meta), (
        "the forwarded presentation did not carry the presented data marker."
    )

    # Sanity: the present op's step ack is the compact reached-user stats line,
    # NOT the data — so the caller-outbox marker above is the forwarded RENDER,
    # not the ack echoing the data back onto the caller surface.
    ack = outcome["named_stores"]["ack"]
    assert "Presented to the user" in ack["text"]
    assert _MARKER not in json.dumps(ack), (
        "the present ack unexpectedly carried the data — the outbox marker match "
        "must prove the forwarded RENDER, not an ack echo."
    )
