"""Tier 3 (e2e): skill-level postprocessor — full OSRuntime pipeline.

Pins the user-facing guarantee: when a Skill declares a postprocessor block,
OSRuntime.run() returns the *postprocessor's* caller-contract artifact rather
than the raw LLM finish artifact.

Two scenarios:

  test_e2e_postprocessor_python_adds_word_count
    — A single-phase skill with a python postprocessor step that appends
      ``word_count`` to the LLM output. Asserts that:
      1. run() returns data containing ``word_count`` (postprocessor output).
      2. The raw LLM artifact (``{title, body}`` only) was NOT returned.
      3. postprocessor_step_started / postprocessor_step_completed events
         are emitted in the EventLog.

  test_e2e_postprocessor_validate_failure_aborts_workflow
    — A single-phase skill whose postprocessor validate step rejects the LLM
      output (requires a field absent in the artifact). Asserts that
      WorkflowAbortedError is raised (the OS surfaces the PostprocessorError
      as an abort so callers see a clean boundary).

Both tests use a hand-coded _ScriptedLLM (a real async callable, not a Mock)
that returns one canned finish response. No cassette file is needed; the
fixture is entirely self-contained.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import reyn.kernel.runtime as runtime_mod
from reyn.kernel.runtime import OSRuntime, RunResult
from reyn.kernel.postprocessor_executor import PostprocessorError
from reyn.llm.llm import LLMCallResult
from reyn.llm.pricing import TokenUsage
from reyn.permissions.permissions import PermissionDecl, PythonPermission
from reyn.schemas.models import (
    Phase,
    Postprocessor,
    Skill,
    SkillGraph,
)


# ---------------------------------------------------------------------------
# Tiny scripted LLM — a real callable, not a Mock.
# Replays a single canned finish response; raises on over-call.
# ---------------------------------------------------------------------------


class _ScriptedLLM:
    """Replay a fixed list of LLM responses; count invocations."""

    def __init__(self, script: list[dict]) -> None:
        self._script = script
        self.call_count = 0

    async def __call__(self, model: str, frame: Any, *args: Any, **kwargs: Any) -> LLMCallResult:
        idx = self.call_count
        self.call_count += 1
        if idx >= len(self._script):
            raise RuntimeError(
                f"LLM script exhausted (call {idx}, "
                f"{len(self._script)} scripted)",
            )
        return LLMCallResult(data=self._script[idx], usage=TokenUsage(10, 20))


# ---------------------------------------------------------------------------
# Shared skill builder helpers
# ---------------------------------------------------------------------------


def _post_phase() -> Phase:
    """Single-phase skill entry: purely finish-capable, no side-effect ops."""
    return Phase(
        name="write",
        instructions="Write a short post. Output {title, body}.",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=[],
        max_act_turns=0,
    )


# One finish turn: LLM returns {title, body}
_FINISH_SCRIPT = [
    {
        "type": "decide",
        "control": {
            "type": "finish",
            "decision": "finish",
            "next_phase": None,
            "confidence": 1.0,
            "reason": {"summary": "done"},
        },
        "artifact": {
            "type": "post_draft",
            "data": {"title": "Hello World", "body": "This is a test post body."},
        },
        "ops": [],
    },
]

# ---------------------------------------------------------------------------
# Test 1: python postprocessor step adds word_count
# ---------------------------------------------------------------------------


def test_e2e_postprocessor_python_adds_word_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 3a: python postprocessor step → caller receives {title, body, word_count}.

    Pins that OSRuntime._finish_workflow hands the LLM artifact to
    PostprocessorExecutor and returns the enriched caller-contract artifact,
    NOT the raw {title, body} from the LLM.

    LLM fixture: inline _ScriptedLLM (one decide-turn finish, no cassette file).
    Postprocessor: python step invoking ./helpers.py:count_words that
    places an integer at data.word_count.
    """
    monkeypatch.chdir(tmp_path)

    # Write the helper module alongside skill fixtures in tmp_path
    helpers_py = tmp_path / "helpers.py"
    helpers_py.write_text(
        "def count_words(artifact):\n"
        "    body = artifact.get('data', {}).get('body', '')\n"
        "    return len(body.split())\n",
        encoding="utf-8",
    )

    # Skill: LLM contract = {title, body}
    #        Postprocessor output_schema = {title, body, word_count}
    phase = _post_phase()
    python_perm = PythonPermission(module="./helpers.py", function="count_words", mode="pure")
    skill = Skill(
        name="post_writer",
        entry_phase="write",
        phases={"write": phase},
        graph=SkillGraph(transitions={}, can_finish_phases=["write"]),
        final_output_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["title", "body"],
        },
        final_output_name="post_draft",
        postprocessor=Postprocessor(
            output_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "word_count": {"type": "integer"},
                },
                "required": ["title", "body", "word_count"],
            },
            output_name="post_draft",
            steps=[
                {
                    "type": "python",
                    "module": "./helpers.py",
                    "function": "count_words",
                    "into": "data.word_count",
                    "output_schema": {"type": "integer"},
                },
            ],
        ),
        permissions=PermissionDecl(python=[python_perm]),
        skill_dir=str(tmp_path),
    )

    # Wire scripted LLM
    llm = _ScriptedLLM(_FINISH_SCRIPT)
    monkeypatch.setattr(runtime_mod, "call_llm", llm)

    collected_events: list[Any] = []

    rt = OSRuntime(
        skill,
        model="stub/model",
        subscribers=[lambda e: collected_events.append(e)],
    )
    result = asyncio.run(rt.run({"type": "input", "data": {}}))

    # ── Core assertions ────────────────────────────────────────────────

    assert isinstance(result, RunResult)
    assert result.ok, f"expected finished, got {result.status}"

    # Postprocessor output — word_count must be present
    assert "word_count" in result.data, (
        f"caller-contract artifact must include word_count; got keys: {list(result.data.keys())}"
    )
    # "This is a test post body." → 6 words
    assert result.data["word_count"] == 6, (
        f"expected word_count=6 for 'This is a test post body.'; got {result.data['word_count']}"
    )

    # LLM artifact fields are also preserved
    assert result.data["title"] == "Hello World"
    assert result.data["body"] == "This is a test post body."

    # ── Event assertions ───────────────────────────────────────────────

    event_types = [e.type for e in collected_events]
    assert "postprocessor_step_started" in event_types, (
        f"postprocessor_step_started missing; events: {event_types}"
    )
    assert "postprocessor_step_completed" in event_types, (
        f"postprocessor_step_completed missing; events: {event_types}"
    )

    started = [e for e in collected_events if e.type == "postprocessor_step_started"]
    completed = [e for e in collected_events if e.type == "postprocessor_step_completed"]
    assert len(started) == 1
    assert len(completed) == 1
    assert started[0].data["step_index"] == 0
    assert completed[0].data["step_index"] == 0


# ---------------------------------------------------------------------------
# Test 2: postprocessor validate failure → WorkflowAbortedError
# ---------------------------------------------------------------------------


def test_e2e_postprocessor_validate_failure_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 3a: postprocessor validate step rejects LLM output → PostprocessorError.

    The LLM produces a valid {title, body} artifact. The postprocessor
    validate step requires 'required_extra' which is absent. PostprocessorError
    propagates out of _finish_workflow (it is not caught in OSRuntime.run())
    so the caller sees an unambiguous failure and does NOT receive partial output.

    Also pins: postprocessor_step_failed event is emitted before the exception
    surfaces so the operator can audit which step rejected the artifact.
    """
    monkeypatch.chdir(tmp_path)

    phase = _post_phase()
    skill = Skill(
        name="post_writer_bad_post",
        entry_phase="write",
        phases={"write": phase},
        graph=SkillGraph(transitions={}, can_finish_phases=["write"]),
        final_output_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["title", "body"],
        },
        final_output_name="post_draft",
        postprocessor=Postprocessor(
            output_schema={
                "type": "object",
                "properties": {
                    "required_extra": {"type": "string"},
                },
                "required": ["required_extra"],
            },
            steps=[
                {
                    "type": "validate",
                    "schema": {
                        "type": "object",
                        "required": ["required_extra"],
                        "properties": {"required_extra": {"type": "string"}},
                    },
                },
            ],
        ),
    )

    llm = _ScriptedLLM(_FINISH_SCRIPT)
    monkeypatch.setattr(runtime_mod, "call_llm", llm)

    rt = OSRuntime(skill, model="stub/model")

    with pytest.raises(PostprocessorError, match=r"step\[0\]"):
        asyncio.run(rt.run({"type": "input", "data": {}}))

    # postprocessor_step_failed event must have been emitted before the raise
    failed_events = [e for e in rt.events.all() if e.type == "postprocessor_step_failed"]
    assert len(failed_events) == 1, (
        f"expected 1 postprocessor_step_failed event; got {len(failed_events)}"
    )
    assert failed_events[0].data["step_index"] == 0
