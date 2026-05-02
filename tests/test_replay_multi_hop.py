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
    ControlIROpSpec,
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
