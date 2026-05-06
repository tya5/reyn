"""Replay tests for multi-hop agent delegation.

Verifies that:
1. Agent A can classify a task as a delegation and produce a
   ``messages_to_agents`` entry with correct chain metadata.
2. Drift detection: changing chain_id changes the fixture key and raises
   MissingFixture — verifying that chain_id is correctly included in the
   prompt hash.

Tier 3a: one typical case + one drift detection per area.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.llm.llm import call_llm
from reyn.schemas.models import (
    CandidateOutput,
    ContextFrame,
    ExecutionState,
    PhaseConstraints,
)
from reyn.testing.replay import REPLAY_DATETIME

MODEL = "gemini-2.5-flash-lite"
SKILL_DESC = (
    "Route a single user chat utterance to an appropriate skill (or reply directly).\n"
    "Used by reyn chat to turn natural language into a routing decision."
)


def _run(coro):
    return asyncio.run(coro)


def _candidate_finish(schema: str = "routing_decision") -> CandidateOutput:
    return CandidateOutput(
        next_phase="end",
        control_type="finish",
        schema_name=schema,
        artifact_schema={
            "type": "object",
            "properties": {
                "reply_text": {"type": "string"},
                "skills_to_run": {"type": "array", "items": {}},
            },
            "required": ["reply_text", "skills_to_run"],
        },
        description="Direct reply for chitchat/stable_knowledge/memory_recall/clarification",
    )


def _candidate_delegate() -> CandidateOutput:
    return CandidateOutput(
        next_phase="end",
        control_type="finish",
        schema_name="delegation_result",
        artifact_schema={
            "type": "object",
            "properties": {
                "reply_text": {"type": "string"},
                "messages_to_agents": {"type": "array", "items": {}},
            },
            "required": ["reply_text", "messages_to_agents"],
        },
        description="Delegate to another agent",
    )


def _agent_a_frame(chain_id: str) -> ContextFrame:
    """Build the canonical Agent A delegation frame with the given chain_id."""
    return ContextFrame(
        current_phase="classify",
        current_phase_role="chat_router",
        instructions="Classify the user intent and delegate to another agent if appropriate.",
        candidate_outputs=[_candidate_finish(), _candidate_delegate()],
        finish_criteria=["Classified and delegated"],
        constraints=PhaseConstraints(),
        available_control_ops=[],
        op_catalog=[],
        output_language="en",
        model="openai/gemini-2.5-flash-lite",
        model_resolved=MODEL,
        input_artifact={
            "type": "chat_routing_request",
            "data": {
                "user_message": "Ask the research agent to find recent papers on quantum computing.",
                "chat_id": "test-agent-a",
                "available_skills": [],
                "available_agents": [
                    {
                        "name": "agent_b",
                        "role": "Specialized research agent for academic papers.",
                    }
                ],
                "history": [],
                "chain_id": chain_id,
            },
        },
        execution=ExecutionState(path=[], current_visit=1, total_steps=0),
        control_ir_results=[],
        remaining_act_turns=2,
        current_datetime=REPLAY_DATETIME,
    )


# ── test: agent A delegates to agent B with chain_id in artifact ──────────────


@pytest.mark.replay("fixtures/llm/multi_hop/agent_delegation.jsonl")
def test_agent_a_produces_delegation_with_chain_id():
    """Tier 3a: Agent A classifies a research delegation and emits messages_to_agents."""
    result = _run(
        call_llm(
            MODEL,
            _agent_a_frame("chain-abc123"),
            prompt_cache_enabled=False,
            skill_name="skill_router",
            skill_description=SKILL_DESC,
            phase_role="chat_router",
        )
    )

    data = result.data
    assert data["type"] == "decide"
    ctrl = data["control"]
    assert ctrl["decision"] in ("finish", "continue")

    art_data = data["artifact"]["data"]
    messages_to_agents = art_data.get("messages_to_agents", [])
    assert len(messages_to_agents) >= 1, (
        "Agent A should produce at least one delegation message to agent_b"
    )
    assert messages_to_agents[0]["to"] == "agent_b"
    assert len(messages_to_agents[0]["request"]) > 0


# ── test: chain_id is included in the fixture key (drift detection) ───────────


@pytest.mark.replay("fixtures/llm/multi_hop/agent_delegation.jsonl")
def test_chain_id_in_input_affects_fixture_key():
    """Tier 3a drift detection: changing chain_id changes the prompt hash → MissingFixture.

    Protects: chain_id must be part of the ContextFrame hash so that a
    different chain_id produces a different fixture key. If the hash did not
    include chain_id, a prompt drift in chain-propagation logic would pass
    silently.
    """
    from reyn.testing.replay import MissingFixture

    with pytest.raises(MissingFixture, match="No fixture entry"):
        _run(
            call_llm(
                MODEL,
                _agent_a_frame("chain-DIFFERENT"),  # different chain_id → different key
                prompt_cache_enabled=False,
                skill_name="skill_router",
                skill_description=SKILL_DESC,
                phase_role="chat_router",
            )
        )


# ── corner case: cycle A → B → A (single-call view) ──────────────────────────


@pytest.mark.xfail(
    reason=(
        "DOGFOOD BUG (med): the router-with-cycle-instruction emits "
        "control.type='finish' with a reason string saying 'finish directly to "
        "break the loop', but the artifact still includes a delegation to "
        "agent_b. Output is internally contradictory — reason says one thing, "
        "artifact does another. Removing this xfail when the prompt is "
        "tightened or post-validation rejects contradictory artifacts."
    ),
    strict=True,
)
@pytest.mark.replay("fixtures/llm/multi_hop/cycle_a_b_a.jsonl")
def test_agent_a_refuses_self_loop_in_chain():
    """Tier 3a corner: Agent A sees a chain that already includes itself → must not delegate again.

    Single-LLM-call exercise: the chain history shown to Agent A indicates
    it already participated upstream. A correct multi-hop policy should not
    re-delegate to agent_b (which would close the cycle); instead the agent
    should either finish directly (chitchat-style decline) or surface the
    loop. Multi-call cycle detection (PR18 chain timeout actually firing)
    needs Tier 3b — flagged in PR28 plan.
    """
    frame = ContextFrame(
        current_phase="classify",
        current_phase_role="chat_router",
        instructions=(
            "Classify the user intent. If chain history already shows this agent has "
            "participated upstream and the request is asking us to delegate further, "
            "treat it as a loop and finish directly without delegation."
        ),
        candidate_outputs=[_candidate_finish(), _candidate_delegate()],
        finish_criteria=["Classified and delegated"],
        constraints=PhaseConstraints(),
        available_control_ops=[],
        op_catalog=[],
        output_language="en",
        model="openai/gemini-2.5-flash-lite",
        model_resolved=MODEL,
        input_artifact={
            "type": "chat_routing_request",
            "data": {
                "user_message": "Please delegate to research_agent for the next step.",
                "chat_id": "test-cycle-001",
                "available_skills": [],
                "available_agents": [
                    {
                        "name": "agent_b",
                        "role": "Specialized research agent for academic papers.",
                    }
                ],
                "history": [],
                "chain_id": "chain-cycle-1",
                "chain_history": [
                    {"agent": "agent_a", "step": 1},
                    {"agent": "agent_b", "step": 2},
                    {"agent": "agent_a", "step": 3, "note": "current"},
                ],
            },
        },
        execution=ExecutionState(path=[], current_visit=1, total_steps=0),
        control_ir_results=[],
        remaining_act_turns=2,
        current_datetime=REPLAY_DATETIME,
    )

    result = _run(
        call_llm(
            MODEL,
            frame,
            prompt_cache_enabled=False,
            skill_name="skill_router",
            skill_description=SKILL_DESC,
            phase_role="chat_router",
        )
    )

    data = result.data
    assert data["type"] == "decide"
    art_data = data["artifact"]["data"]
    # If it does emit a delegation, it should NOT delegate back to agent_b
    # (closing the cycle). Most natural: finish directly.
    msgs = art_data.get("messages_to_agents", [])
    if msgs:
        for m in msgs:
            # accept either standard "to" or the alternate "agent_name" field
            target = m.get("to") or m.get("agent_name")
            assert target != "agent_b", (
                f"Agent A should not delegate to agent_b when the chain already "
                f"shows agent_b → agent_a; got messages_to_agents={msgs}"
            )


# ── corner case: chain timeout signal mid-relay ─────────────────────────────


@pytest.mark.replay("fixtures/llm/multi_hop/chain_timeout_mid_relay.jsonl")
def test_agent_acknowledges_imminent_chain_timeout():
    """Tier 3a corner: chain metadata flags timeout imminent → agent must not start a new relay.

    Multi-call timeout firing during a real LLM call belongs to Tier 3b
    (cannot be reproduced in a single call). We pin the simpler invariant:
    when the chain budget is signalled exhausted, the agent does not emit
    a fresh outbound delegation.
    """
    frame = ContextFrame(
        current_phase="classify",
        current_phase_role="chat_router",
        instructions=(
            "Classify the user intent. If chain_status indicates the chain is "
            "exhausted or timing out, finish directly with a short reply that "
            "says you cannot continue the relay."
        ),
        candidate_outputs=[_candidate_finish(), _candidate_delegate()],
        finish_criteria=["Classified"],
        constraints=PhaseConstraints(),
        available_control_ops=[],
        op_catalog=[],
        output_language="en",
        model="openai/gemini-2.5-flash-lite",
        model_resolved=MODEL,
        input_artifact={
            "type": "chat_routing_request",
            "data": {
                "user_message": "Please delegate the deeper analysis to a downstream agent.",
                "chat_id": "test-timeout-001",
                "available_skills": [],
                "available_agents": [
                    {
                        "name": "agent_b",
                        "role": "Downstream relay agent.",
                    }
                ],
                "history": [],
                "chain_id": "chain-timeout-1",
                "chain_status": "exhausted",
                "chain_budget_remaining": 0,
            },
        },
        execution=ExecutionState(path=[], current_visit=1, total_steps=0),
        control_ir_results=[],
        remaining_act_turns=2,
        current_datetime=REPLAY_DATETIME,
    )

    result = _run(
        call_llm(
            MODEL,
            frame,
            prompt_cache_enabled=False,
            skill_name="skill_router",
            skill_description=SKILL_DESC,
            phase_role="chat_router",
        )
    )

    data = result.data
    assert data["type"] == "decide"
    art_data = data["artifact"]["data"]
    # When chain budget is exhausted, the agent must not start a new delegation.
    msgs = art_data.get("messages_to_agents", [])
    assert len(msgs) == 0, (
        f"Agent should not delegate when chain_status=exhausted; got {msgs}"
    )
