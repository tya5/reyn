"""Replay tests for multi-hop agent delegation.

Verifies that:
1. Agent A can classify a task as a delegation to agent B and produce a
   ``messages_to_agents`` entry with the correct chain metadata.
2. Agent B (deferred reply path) receives the delegated request and responds
   with a meaningful answer — the fixture carries the same ``chain_id`` to
   verify chain propagation without needing the full AgentRegistry.

The tests call ``call_llm`` directly with ``ContextFrame`` objects that mirror
what the runtime sends during multi-hop exchanges.  The ``chain_id`` field in
``input_artifact.data`` is what the runtime injects; the LLM output must be
stable (i.e. the fixture key matches).
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


# ── test: agent A delegates to agent B with chain_id in artifact ──────────────


@pytest.mark.replay("fixtures/llm/multi_hop/agent_delegation.jsonl")
def test_agent_a_produces_delegation_with_chain_id():
    """Agent A classifies a research delegation and emits messages_to_agents."""
    frame = ContextFrame(
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
                "chain_id": "chain-abc123",
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
    ctrl = data["control"]
    assert ctrl["decision"] in ("finish", "continue")

    artifact = data["artifact"]
    art_data = artifact["data"]

    # The routing_decision may embed delegation in messages_to_agents
    messages_to_agents = art_data.get("messages_to_agents", [])
    assert len(messages_to_agents) >= 1, (
        "Agent A should produce at least one delegation message to agent_b"
    )
    assert messages_to_agents[0]["to"] == "agent_b"
    assert len(messages_to_agents[0]["request"]) > 0


# ── test: agent B receives delegated request (deferred reply path) ────────────


@pytest.mark.replay("fixtures/llm/multi_hop/deferred_reply.jsonl")
def test_agent_b_handles_deferred_delegation():
    """Agent B receives a delegated request with chain_id and produces a reply."""
    frame = ContextFrame(
        current_phase="classify",
        current_phase_role="chat_router",
        instructions="Classify the incoming delegation request.",
        candidate_outputs=[_candidate_finish()],
        finish_criteria=["Request handled"],
        constraints=PhaseConstraints(),
        available_control_ops=[],
        op_catalog=[],
        output_language="en",
        model="openai/gemini-2.5-flash-lite",
        model_resolved=MODEL,
        input_artifact={
            "type": "chat_routing_request",
            "data": {
                "user_message": "Find recent papers on quantum computing.",
                "chat_id": "agent_b",
                "available_skills": [],
                "history": [],
                "chain_id": "chain-abc123",
                "reply_to": "test-agent-a",
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
    ctrl = data["control"]
    assert ctrl["type"] == "finish"

    artifact = data["artifact"]
    art_data = artifact["data"]
    # Agent B should reply with content about quantum computing papers
    assert isinstance(art_data["reply_text"], str)
    assert len(art_data["reply_text"]) > 20, (
        "Expected a substantive reply from agent B about quantum computing papers"
    )
    assert art_data["skills_to_run"] == []


# ── test: chain_id is preserved in the fixture key ───────────────────────────


@pytest.mark.replay("fixtures/llm/multi_hop/agent_delegation.jsonl")
def test_chain_id_in_input_affects_fixture_key():
    """Changing chain_id changes the fixture key (prompt drift detection)."""
    from reyn.testing.replay import MissingFixture

    # Frame with a DIFFERENT chain_id — the key won't match the fixture.
    frame_wrong_chain = ContextFrame(
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
                "chain_id": "chain-DIFFERENT",   # <-- different chain id
            },
        },
        execution=ExecutionState(path=[], current_visit=1, total_steps=0),
        control_ir_results=[],
        remaining_act_turns=2,
        current_datetime=REPLAY_DATETIME,
    )

    with pytest.raises(MissingFixture, match="No fixture entry"):
        _run(
            call_llm(
                MODEL,
                frame_wrong_chain,
                prompt_cache_enabled=False,
                skill_name="skill_router",
                skill_description=SKILL_DESC,
                phase_role="chat_router",
            )
        )
