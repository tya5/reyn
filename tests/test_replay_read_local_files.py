"""Replay tests for the `read_local_files` stdlib skill.

Tier 3a, single-call replay. Verifies that the two phases of the skill
produce stable, valid decisions for representative inputs:

- ``decide_files``: classification/planning. Given a user message that
  mentions a path, the LLM transitions to ``read_and_respond`` with a
  ``read_plan`` artifact whose ``paths`` is non-empty.
- ``read_and_respond``: synthesis after MCP reads. Given a populated
  ``read_plan`` plus simulated ``control_ir_results``, the LLM emits a
  decide turn carrying a ``file_content_response``.

Two fixtures cover the happy path and the path-error path. Both are
written as plain JSON (not recorded against a real LLM) — the agent has
no API key in this environment and the brief authorises hand-crafting
fixtures so long as the format documented in
``src/reyn/testing/replay.py`` is honoured.
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
    "Read one or more local project files via a configured filesystem MCP "
    "server, then synthesise an answer that references their contents."
)


def _run(coro):
    return asyncio.run(coro)


# ── Candidate-output builders (mirror the OS-prepared frame) ──────────────────


def _candidate_read_and_respond() -> CandidateOutput:
    return CandidateOutput(
        next_phase="read_and_respond",
        control_type="transition",
        schema_name="read_plan",
        artifact_schema={
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}},
                "reason": {"type": "string"},
            },
            "required": ["paths", "reason"],
        },
        description="Hand off plan to read_and_respond",
    )


def _candidate_finish_response() -> CandidateOutput:
    return CandidateOutput(
        next_phase="end",
        control_type="finish",
        schema_name="file_content_response",
        artifact_schema={
            "type": "object",
            "properties": {
                "response": {"type": "string"},
                "files_read": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["response", "files_read"],
        },
        description="Final answer using file contents",
    )


def _op_mcp() -> ControlIROpSpec:
    return ControlIROpSpec(
        kind="mcp",
        description="Call a tool on a configured MCP HTTP server",
        example={
            "kind": "mcp",
            "server": "filesystem",
            "tool": "read_text_file",
            "args": {"path": "README.md"},
        },
    )


# ── Frame builders ────────────────────────────────────────────────────────────


def _decide_files_frame(user_text: str) -> ContextFrame:
    return ContextFrame(
        current_phase="decide_files",
        current_phase_role="file_planner",
        instructions=(
            "Pick the smallest useful set of project-relative paths to read, "
            "then transition to read_and_respond."
        ),
        candidate_outputs=[_candidate_read_and_respond()],
        finish_criteria=["Plan produced and handed to read_and_respond"],
        constraints=PhaseConstraints(),
        available_control_ops=[],
        op_catalog=[],
        output_language="en",
        model="openai/gemini-2.5-flash-lite",
        model_resolved=MODEL,
        input_artifact={"type": "user_message", "data": {"text": user_text}},
        execution=ExecutionState(path=[], current_visit=1, total_steps=0),
        control_ir_results=[],
        remaining_act_turns=1,
        current_datetime=REPLAY_DATETIME,
    )


def _read_and_respond_frame(
    paths: list[str],
    reason: str,
    control_ir_results: list[dict],
    remaining_act_turns: int,
) -> ContextFrame:
    return ContextFrame(
        current_phase="read_and_respond",
        current_phase_role="file_synthesiser",
        instructions=(
            "Read each path in the plan via the filesystem MCP server, then "
            "compose a single natural-language answer that uses what you found."
        ),
        candidate_outputs=[_candidate_finish_response()],
        finish_criteria=[
            "file_content_response.response is non-empty",
            "files_read lists the paths that were successfully read",
        ],
        constraints=PhaseConstraints(),
        available_control_ops=[_op_mcp()],
        op_catalog=[],
        output_language="en",
        model="openai/gemini-2.5-flash-lite",
        model_resolved=MODEL,
        input_artifact={
            "type": "read_plan",
            "data": {"paths": paths, "reason": reason},
        },
        execution=ExecutionState(
            path=["decide_files → read_and_respond"],
            current_visit=1,
            total_steps=1,
        ),
        control_ir_results=control_ir_results,
        remaining_act_turns=remaining_act_turns,
        current_datetime=REPLAY_DATETIME,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.replay("fixtures/llm/read_local_files/happy_path.jsonl")
def test_decide_files_picks_paths_and_transitions():
    """Tier 3a: decide_files classifies a path-y prompt → transition with a plan.

    The user names a specific path. The phase should propose at least that
    path in the read_plan and transition to read_and_respond.
    """
    frame = _decide_files_frame(
        "Read the README and summarise the philosophy section."
    )
    result = _run(
        call_llm(
            MODEL,
            frame,
            prompt_cache_enabled=False,
            skill_name="read_local_files",
            skill_description=SKILL_DESC,
            phase_role="file_planner",
        )
    )

    data = result.data
    assert data["type"] == "decide"
    ctrl = data["control"]
    assert ctrl["type"] == "transition"
    assert ctrl["decision"] == "continue"
    assert ctrl["next_phase"] == "read_and_respond"

    artifact = data["artifact"]
    assert artifact["type"] == "read_plan"
    plan = artifact["data"]
    assert isinstance(plan["paths"], list) and len(plan["paths"]) >= 1
    # At least one of the returned paths must look README-y for this prompt.
    assert any("README" in p for p in plan["paths"]), (
        f"Expected README in planned paths, got {plan['paths']!r}"
    )
    assert isinstance(plan["reason"], str) and plan["reason"]


@pytest.mark.replay("fixtures/llm/read_local_files/happy_path.jsonl")
def test_read_and_respond_synthesises_after_successful_mcp_read():
    """Tier 3a: read_and_respond, given an OK mcp result, finishes with content.

    The mandatory decide turn (remaining_act_turns=0) sees a successful
    ``mcp`` result in ``control_ir_results``; it must finish with
    ``file_content_response`` referencing the file.
    """
    mcp_ok = {
        "kind": "mcp",
        "status": "ok",
        "server": "filesystem",
        "tool": "read_text_file",
        "content": "# README\n\n## Philosophy\n\nReyn favours predictability.\n",
        "raw": {"isError": False},
    }
    frame = _read_and_respond_frame(
        paths=["README.md"],
        reason="Summarise the philosophy section of the README.",
        control_ir_results=[mcp_ok],
        remaining_act_turns=0,
    )
    result = _run(
        call_llm(
            MODEL,
            frame,
            prompt_cache_enabled=False,
            skill_name="read_local_files",
            skill_description=SKILL_DESC,
            phase_role="file_synthesiser",
        )
    )

    data = result.data
    assert data["type"] == "decide"
    ctrl = data["control"]
    assert ctrl["type"] == "finish"
    assert ctrl["decision"] == "finish"
    assert ctrl["next_phase"] is None

    artifact = data["artifact"]
    assert artifact["type"] == "file_content_response"
    payload = artifact["data"]
    assert isinstance(payload["response"], str) and len(payload["response"]) > 0
    assert payload["files_read"] == ["README.md"]


@pytest.mark.replay("fixtures/llm/read_local_files/path_error.jsonl")
def test_read_and_respond_handles_mcp_error_gracefully():
    """Tier 3a corner: read_and_respond with status=error must not fabricate content.

    Protects against the most common failure mode: the LLM ignores the
    error and synthesises a confident answer about a file it never saw.
    Pinned behaviour: ``files_read`` must NOT include the failed path,
    and ``response`` must mention the failure (not a fabricated summary).
    """
    mcp_err = {
        "kind": "mcp",
        "status": "error",
        "server": "filesystem",
        "tool": "read_text_file",
        "error": "ENOENT: no such file or directory: docs/missing.md",
    }
    frame = _read_and_respond_frame(
        paths=["docs/missing.md"],
        reason="Summarise docs/missing.md.",
        control_ir_results=[mcp_err],
        remaining_act_turns=0,
    )
    result = _run(
        call_llm(
            MODEL,
            frame,
            prompt_cache_enabled=False,
            skill_name="read_local_files",
            skill_description=SKILL_DESC,
            phase_role="file_synthesiser",
        )
    )

    data = result.data
    assert data["type"] == "decide"
    ctrl = data["control"]
    assert ctrl["type"] == "finish"
    assert ctrl["decision"] == "finish"

    artifact = data["artifact"]
    assert artifact["type"] == "file_content_response"
    payload = artifact["data"]
    # No fabricated read recorded.
    assert "docs/missing.md" not in payload["files_read"], (
        "files_read must not list a path whose mcp result was status=error"
    )
    # Response mentions the failure.
    assert any(
        token in payload["response"].lower()
        for token in ("could not", "couldn't", "missing", "error", "not found")
    ), (
        f"Response should acknowledge the read failure, got: {payload['response']!r}"
    )
