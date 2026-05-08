"""Tier 2: chat plan-mode (= multi-step decomposition with narrow LLM
calls per step).

Pins the contract that:

  - The ``plan`` tool's args parse into a typed ``Plan`` model with
    structural validation (= step count bounds, id uniqueness, tool-name
    whitelist, dep-cycle detection, forward-ref handling).
  - Topological-sort produces a stable order matching declaration when
    deps are absent, and respects deps when present.
  - ``_PlanStepHost`` narrows the host catalogue per-step: file tools
    appear only when the step needs them, skills only when invoke_skill
    is in the step's tool list, etc.
  - The narrow system prompt for a step contains the goal + step
    description + prior step results, but not the full chat router
    scaffolding.
  - ``execute_plan`` collects step outputs, falls through failed steps,
    and emits ``plan_*`` events for audit.

Tier 3 (= LLMReplay e2e of a real plan execution) is in
test_planner_e2e.py — kept separate so the unit-level pinning here
runs without provider creds.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from reyn.chat.planner import (
    Plan,
    PlanStep,
    PlanValidationError,
    _PlanStepHost,
    _topological_order,
    build_plan_step_system_prompt,
    parse_and_validate_plan,
)

_ALLOWED = {"reyn_src_read", "reyn_src_list", "web_search", "list_skills", "invoke_skill", "remember_shared", "read_file"}


# ── parse_and_validate_plan ─────────────────────────────────────────────────


def test_parse_minimal_valid_plan():
    """Tier 2: a valid 2-step plan parses into a typed Plan."""
    args = {
        "goal": "summarise README and synthesise",
        "steps_json": json.dumps([
            {"id": "s1", "description": "read README", "tools": ["reyn_src_read"]},
            {"id": "s2", "description": "synthesise", "tools": [], "depends_on": ["s1"]},
        ]),
    }
    plan = parse_and_validate_plan(args, allowed_tool_names=_ALLOWED)
    assert plan.goal == "summarise README and synthesise"
    assert len(plan.steps) == 2
    assert plan.steps[0].id == "s1"
    assert plan.steps[0].tools == ("reyn_src_read",)
    assert plan.steps[1].depends_on == ("s1",)


def test_parse_accepts_legacy_typed_steps_field():
    """Tier 2: forward-compat hatch — if the LLM emits ``steps`` as a
    typed array (= old schema), it still works without re-emit. Wire
    schema currently uses ``steps_json`` (= Gemini-safe), but parser
    accepts both.
    """
    args = {
        "goal": "g",
        "steps": [
            {"id": "a", "description": "first", "tools": []},
            {"id": "b", "description": "second", "tools": []},
        ],
    }
    plan = parse_and_validate_plan(args, allowed_tool_names=_ALLOWED)
    assert len(plan.steps) == 2


def test_parse_rejects_invalid_json_in_steps_json():
    """Tier 2: malformed JSON in steps_json fails validation with a
    clear error (= the LLM can re-emit)."""
    args = {"goal": "g", "steps_json": "not-json"}
    with pytest.raises(PlanValidationError, match="not valid JSON"):
        parse_and_validate_plan(args, allowed_tool_names=_ALLOWED)


def test_parse_rejects_too_few_steps():
    """Tier 2: 1 step is below the min — plan should be redundant when
    there's only one task. The error message points at the alternative
    (= "reply directly or call a single tool")."""
    args = {
        "goal": "g",
        "steps_json": json.dumps([
            {"id": "s1", "description": "only one", "tools": []}
        ]),
    }
    with pytest.raises(PlanValidationError, match="between 2 and 7"):
        parse_and_validate_plan(args, allowed_tool_names=_ALLOWED)


def test_parse_rejects_too_many_steps():
    """Tier 2: 8+ steps above the max — keeps plans tractable."""
    args = {
        "goal": "g",
        "steps_json": json.dumps([
            {"id": f"s{i}", "description": "...", "tools": []}
            for i in range(8)
        ]),
    }
    with pytest.raises(PlanValidationError, match="between 2 and 7"):
        parse_and_validate_plan(args, allowed_tool_names=_ALLOWED)


def test_parse_rejects_duplicate_step_ids():
    """Tier 2: id uniqueness — without it depends_on would be ambiguous."""
    args = {
        "goal": "g",
        "steps_json": json.dumps([
            {"id": "x", "description": "first", "tools": []},
            {"id": "x", "description": "second", "tools": []},
        ]),
    }
    with pytest.raises(PlanValidationError, match="duplicate id"):
        parse_and_validate_plan(args, allowed_tool_names=_ALLOWED)


def test_parse_rejects_unknown_tool_name():
    """Tier 2: a step requesting a tool not in the available catalog
    fails. Plans cannot invent new tools — they can only orchestrate
    what the OS already exposes."""
    args = {
        "goal": "g",
        "steps_json": json.dumps([
            {"id": "s1", "description": "read", "tools": ["nonexistent_tool"]},
            {"id": "s2", "description": "synth", "tools": []},
        ]),
    }
    with pytest.raises(PlanValidationError, match="not in the available tool catalog"):
        parse_and_validate_plan(args, allowed_tool_names=_ALLOWED)


def test_parse_rejects_unknown_depends_on_target():
    """Tier 2: depends_on must reference an id that exists in the plan."""
    args = {
        "goal": "g",
        "steps_json": json.dumps([
            {"id": "s1", "description": "first", "tools": []},
            {"id": "s2", "description": "second", "tools": [], "depends_on": ["s99"]},
        ]),
    }
    with pytest.raises(PlanValidationError, match="references unknown id"):
        parse_and_validate_plan(args, allowed_tool_names=_ALLOWED)


def test_parse_rejects_dependency_cycle():
    """Tier 2: a cycle in depends_on is rejected. Without this, the
    executor would loop forever or stall.
    """
    args = {
        "goal": "g",
        "steps_json": json.dumps([
            {"id": "a", "description": "a", "tools": [], "depends_on": ["b"]},
            {"id": "b", "description": "b", "tools": [], "depends_on": ["a"]},
        ]),
    }
    with pytest.raises(PlanValidationError, match="dependency cycle"):
        parse_and_validate_plan(args, allowed_tool_names=_ALLOWED)


def test_parse_rejects_empty_goal():
    args = {
        "goal": "",
        "steps_json": json.dumps([
            {"id": "s1", "description": "x", "tools": []},
            {"id": "s2", "description": "y", "tools": []},
        ]),
    }
    with pytest.raises(PlanValidationError, match="non-empty"):
        parse_and_validate_plan(args, allowed_tool_names=_ALLOWED)


# ── Topological order ──────────────────────────────────────────────────────


def test_topological_order_simple_chain():
    """Tier 2: a → b → c is sorted in dependency order."""
    steps = [
        PlanStep(id="c", description="3", tools=(), depends_on=("b",)),
        PlanStep(id="a", description="1", tools=()),
        PlanStep(id="b", description="2", tools=(), depends_on=("a",)),
    ]
    out = _topological_order(steps)
    assert [s.id for s in out] == ["a", "b", "c"]


def test_topological_order_independent_steps_preserve_emission_order():
    """Tier 2: when deps are absent, the LLM-emitted order is preserved
    (= stable). Predictable execution order makes the events log
    readable without a graph viewer.
    """
    steps = [
        PlanStep(id="x", description="1", tools=()),
        PlanStep(id="y", description="2", tools=()),
        PlanStep(id="z", description="3", tools=()),
    ]
    out = _topological_order(steps)
    assert [s.id for s in out] == ["x", "y", "z"]


# ── Narrow system prompt ────────────────────────────────────────────────────


def test_step_system_prompt_includes_goal_and_step_description():
    """Tier 2: the narrow prompt has the plan goal + step description.
    These are the minimum context the step LLM needs to know what
    to produce.
    """
    plan = Plan(
        goal="multi-source synthesis on Reyn architecture",
        steps=(
            PlanStep(id="s1", description="read principles.md", tools=("reyn_src_read",)),
            PlanStep(id="s2", description="synthesise", tools=(), depends_on=("s1",)),
        ),
    )
    prompt = build_plan_step_system_prompt(plan, plan.steps[0], {})
    assert "multi-source synthesis on Reyn architecture" in prompt
    assert "read principles.md" in prompt
    assert "s1" in prompt


def test_step_system_prompt_includes_prior_step_results_when_deps_present():
    """Tier 2: a step with deps gets the prior step's text in its
    system prompt as ``Prior step results``. The terminal/synthesis
    step uses these to compose the user-facing reply.
    """
    plan = Plan(
        goal="g",
        steps=(
            PlanStep(id="s1", description="read", tools=("reyn_src_read",)),
            PlanStep(id="s2", description="synth", tools=(), depends_on=("s1",)),
        ),
    )
    prior = {"s1": "README says Reyn is an LLM workflow OS."}
    prompt = build_plan_step_system_prompt(plan, plan.steps[1], prior)
    assert "Prior step results" in prompt
    assert "README says Reyn is an LLM workflow OS." in prompt


def test_step_system_prompt_omits_prior_results_when_no_deps():
    """Tier 2: a step without deps has no Prior section (= avoids
    polluting the prompt with empty boilerplate)."""
    plan = Plan(
        goal="g",
        steps=(
            PlanStep(id="s1", description="independent", tools=()),
            PlanStep(id="s2", description="other", tools=()),
        ),
    )
    prompt = build_plan_step_system_prompt(plan, plan.steps[0], {})
    assert "Prior step results" not in prompt


def test_step_system_prompt_smaller_than_full_router_prompt():
    """Tier 2: a step prompt is much smaller than the full chat router
    prompt (= the whole point of plan mode is per-call context shrink).
    Lower bound: 2x smaller. Pre-fix the full router prompt was
    ~7500 chars; a 4-step plan's per-step prompt should be <2000.
    """
    plan = Plan(
        goal="moderately long goal " * 5,
        steps=tuple(
            PlanStep(id=f"s{i}", description="moderate description " * 3, tools=())
            for i in range(4)
        ),
    )
    prompt = build_plan_step_system_prompt(plan, plan.steps[0], {})
    assert len(prompt) < 2000, (
        f"step prompt is {len(prompt)} chars; should be much smaller than "
        "the full router prompt to be worth the per-step LLM call"
    )


# ── _PlanStepHost catalog narrowing ─────────────────────────────────────────


class _FakeParentHost:
    """Minimal stub of RouterLoopHost for narrowing tests."""

    chat_id = "test"
    agent_name = "test_agent"
    agent_role = "test_role"
    output_language = None
    events = type("Ev", (), {"emit": lambda self, *a, **kw: None})()

    def __init__(self):
        self._skills = [{"name": "s1"}, {"name": "s2"}]
        self._agents = [{"name": "a1", "role": "r"}]

    def list_available_skills(self): return list(self._skills)
    def list_available_agents(self): return list(self._agents)
    def get_memory_index(self): return {"status": "ok", "content": "mem"}
    def get_file_permissions(self): return {"read": ["docs"], "write": []}
    def get_mcp_servers(self): return [{"name": "mcp1"}]
    def get_web_fetch_allowed(self): return True
    def get_project_context(self): return "project ctx"
    def memory_path(self, layer, slug): return f"/{layer}/{slug}"
    def memory_dir(self, layer): return f"/{layer}"


def test_narrow_host_hides_skills_when_step_doesnt_need_them():
    """Tier 2: if step.tools doesn't include invoke_skill /
    describe_skill, the narrow host returns no skills. The step's
    catalog is leaner — the LLM can't accidentally pick a skill it
    wasn't supposed to use."""
    parent = _FakeParentHost()
    plan = Plan(
        goal="g",
        steps=(
            PlanStep(id="a", description="x", tools=("reyn_src_read",)),
            PlanStep(id="b", description="y", tools=()),
        ),
    )
    host = _PlanStepHost(plan=plan, step=plan.steps[0], prior_results={}, parent=parent)
    assert host.list_available_skills() == []


def test_narrow_host_passes_through_skills_when_invoke_in_tools():
    """Tier 2: if invoke_skill is in step.tools, the parent's full
    skill list passes through (= the step might invoke any skill)."""
    parent = _FakeParentHost()
    plan = Plan(
        goal="g",
        steps=(
            PlanStep(id="a", description="x", tools=("invoke_skill",)),
            PlanStep(id="b", description="y", tools=()),
        ),
    )
    host = _PlanStepHost(plan=plan, step=plan.steps[0], prior_results={}, parent=parent)
    assert host.list_available_skills() == [{"name": "s1"}, {"name": "s2"}]


def test_narrow_host_hides_file_perms_when_no_file_tools():
    """Tier 2: if no file tools in step.tools, the host returns None
    for file perms — file_* tools won't appear in the step's catalog.
    """
    parent = _FakeParentHost()
    plan = Plan(
        goal="g",
        steps=(
            PlanStep(id="a", description="x", tools=("web_search",)),
            PlanStep(id="b", description="y", tools=()),
        ),
    )
    host = _PlanStepHost(plan=plan, step=plan.steps[0], prior_results={}, parent=parent)
    assert host.get_file_permissions() is None


def test_narrow_host_passes_file_perms_when_read_file_in_tools():
    parent = _FakeParentHost()
    plan = Plan(
        goal="g",
        steps=(
            PlanStep(id="a", description="x", tools=("read_file",)),
            PlanStep(id="b", description="y", tools=()),
        ),
    )
    host = _PlanStepHost(plan=plan, step=plan.steps[0], prior_results={}, parent=parent)
    assert host.get_file_permissions() == {"read": ["docs"], "write": []}


def test_narrow_host_silences_project_context():
    """Tier 2: project context is narrowed out per-step. Plan steps
    work from the step description, not project-wide background — that
    background is what the planner saw when it emitted the plan, not
    something each step needs to re-read.
    """
    parent = _FakeParentHost()
    plan = Plan(
        goal="g",
        steps=(
            PlanStep(id="a", description="x", tools=()),
            PlanStep(id="b", description="y", tools=()),
        ),
    )
    host = _PlanStepHost(plan=plan, step=plan.steps[0], prior_results={}, parent=parent)
    assert host.get_project_context() == ""


def test_narrow_host_captures_agent_text_outbox():
    """Tier 2: when the step LLM emits ``put_outbox(kind="agent", text=...)``,
    the host captures it (= what the executor later reads as the step's
    contribution). Status / trace kinds are dropped silently."""
    parent = _FakeParentHost()
    plan = Plan(
        goal="g",
        steps=(
            PlanStep(id="a", description="x", tools=()),
            PlanStep(id="b", description="y", tools=()),
        ),
    )
    host = _PlanStepHost(plan=plan, step=plan.steps[0], prior_results={}, parent=parent)
    asyncio.run(host.put_outbox(kind="status", text="thinking", meta={}))
    assert host.captured_text == ""
    asyncio.run(host.put_outbox(kind="agent", text="step result text", meta={}))
    assert host.captured_text == "step result text"
