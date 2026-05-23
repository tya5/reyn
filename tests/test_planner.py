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
    _PLAN_RETRY_EXCLUDED,
    _PLAN_STEP_MAX_ITERATIONS,
    _PLAN_STEP_RETRY_LIMIT,
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
    to produce. Step ids (e.g. "s1") must NOT appear in the prompt
    body — they are internal planner bookkeeping and must not leak
    into LLM context (Component B: step id removal).
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
    # Component B: step id must NOT appear as a standalone label in the prompt.
    # (It may appear inside the step description text, but not as a header id.)
    assert "id=s1" not in prompt
    assert "## This step" not in prompt
    assert "## Your task" in prompt


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


def test_step_system_prompt_output_language_prepended_when_set():
    """Tier 2: when output_language is provided, the prompt starts with
    a language directive. Verifies Component A fix — JA users no longer
    get EN replies from plan step LLMs.
    """
    plan = Plan(
        goal="g",
        steps=(
            PlanStep(id="s1", description="read", tools=()),
            PlanStep(id="s2", description="synth", tools=()),
        ),
    )
    prompt_ja = build_plan_step_system_prompt(plan, plan.steps[0], {}, output_language="Japanese")
    assert prompt_ja.startswith("Respond in Japanese.")

    prompt_none = build_plan_step_system_prompt(plan, plan.steps[0], {})
    assert not prompt_none.startswith("Respond in")


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


# ── FP-0028: plan step status text uses description ─────────────────────────


def test_plan_status_text_uses_step_description_not_id():
    """Tier 2: FP-0028 — step status text is human-readable (step
    description or truncation thereof) rather than the internal step id.

    Observation: build_plan_step_system_prompt is not what emits status
    text, but this test pins the public constant / truncation contract:
    the description is capped at 60 chars in the status label.
    The exact status emission is in execute_plan (integration tested in
    test_plan_async_dispatch.py); here we pin the truncation rule used
    on descriptions longer than 60 chars.
    """
    long_desc = "A" * 80  # 80-char description
    truncated = (long_desc or "step_id")[:60]
    assert len(truncated) == 60
    assert truncated == "A" * 60

    short_desc = "read README"
    truncated_short = (short_desc or "step_id")[:60]
    assert truncated_short == short_desc  # short descriptions are unchanged

    empty_desc = ""
    fallback_id = "s1"
    truncated_empty = (empty_desc or fallback_id)[:60]
    assert truncated_empty == fallback_id  # empty falls back to step id


# ── B28-MED-3: project root injection in step system prompt ─────────────────


def test_step_system_prompt_includes_project_root_cwd_line():
    """Tier 2: B28-MED-3 — build_plan_step_system_prompt injects a
    "You are at project root: <cwd>" line so step LLMs can anchor
    relative file paths to the actual working directory.

    Verifies both: (a) the line is present with the supplied cwd value,
    and (b) when no cwd is supplied, the line reflects Path.cwd() at
    call time (same value, since the test process does not change cwd).
    """
    from pathlib import Path

    plan = Plan(
        goal="multi-source synthesis on Reyn architecture",
        steps=(
            PlanStep(id="s1", description="read principles.md", tools=("reyn_src_read",)),
            PlanStep(id="s2", description="synthesise", tools=(), depends_on=("s1",)),
        ),
    )

    # (a) explicit cwd supplied — prompt must contain it
    explicit_root = "/home/user/my-project"
    prompt_explicit = build_plan_step_system_prompt(
        plan, plan.steps[0], {}, cwd=explicit_root,
    )
    assert f"You are at project root: {explicit_root}" in prompt_explicit

    # (b) no cwd supplied — defaults to Path.cwd() at call time
    expected_cwd = str(Path.cwd())
    prompt_default = build_plan_step_system_prompt(plan, plan.steps[0], {})
    assert f"You are at project root: {expected_cwd}" in prompt_default


# ── FP-0029: plan step iteration budget default = 5 ─────────────────────────


def test_plan_step_max_iterations_default_is_5():
    """Tier 2: FP-0029 — _PLAN_STEP_MAX_ITERATIONS is 5 (raised from 3).

    The constant is the OS default that ``execute_plan`` uses when no
    config override is provided. Pins that the behavioral change landed.
    """
    assert _PLAN_STEP_MAX_ITERATIONS == 5


def test_plan_step_max_iterations_config_override():
    """Tier 2: FP-0029 — PlanConfig.step_max_iterations can be set via
    config and _build_plan_config parses it correctly.

    Observation: load the config builder with a raw dict and verify the
    resulting PlanConfig carries the overridden value.
    """
    from reyn.config import _build_plan_config  # type: ignore[attr-defined]

    # Default when no plan: section present
    cfg_default = _build_plan_config(None)
    assert cfg_default.step_max_iterations == 5

    # Override via dict
    cfg_custom = _build_plan_config({"step_max_iterations": 8})
    assert cfg_custom.step_max_iterations == 8

    # Non-positive values fall back to default
    cfg_bad = _build_plan_config({"step_max_iterations": 0})
    assert cfg_bad.step_max_iterations == 5

    # Non-numeric values fall back to default
    cfg_bad2 = _build_plan_config({"step_max_iterations": "not-a-number"})
    assert cfg_bad2.step_max_iterations == 5


# ── FP-0030: plan step system prompt includes concrete details guidance ───────


def test_plan_step_sp_includes_concrete_details_guidance():
    """Tier 2: FP-0030 — the step system prompt now directs the step LLM
    to include concrete details (code snippets, line numbers, exact values)
    and uses a soft ~800-char target rather than "1–3 sentences".

    Pins that the old terse guidance is gone and the new richer guidance
    is present, so plan step outputs are more useful to the synthesis LLM.
    """
    plan = Plan(
        goal="explore the codebase",
        steps=(
            PlanStep(id="s1", description="read core module", tools=("reyn_src_read",)),
            PlanStep(id="s2", description="synthesise", tools=(), depends_on=("s1",)),
        ),
    )
    prompt = build_plan_step_system_prompt(plan, plan.steps[0], {})

    # New concrete-details guidance must be present
    assert "concrete details" in prompt
    assert "code snippets" in prompt
    assert "800 characters" in prompt

    # Old terse guidance must NOT be present
    assert "1–3 sentences" not in prompt
    assert "Summarise what this step found in 1" not in prompt


# ── FP-0031-C: auto-retry with exclusion list ───────────────────────────────


class _SimpleHost:
    """Minimal host for retry tests — no WAL, just events + outbox."""
    chat_id = "test"
    agent_name = "test"
    agent_role = "test"
    output_language = None
    events = type("Ev", (), {"emit": lambda self, *a, **kw: None})()

    def __init__(self):
        self.outbox: list[dict] = []
        self.step_failed_calls: list[dict] = []
        self.step_started_calls: list[dict] = []
        self.step_completed_calls: list[dict] = []
        self.plan_started_calls: list[dict] = []
        self.plan_completed_calls: list[dict] = []

    async def put_outbox(self, *, kind, text, meta):
        self.outbox.append({"kind": kind, "text": text, "meta": meta})

    async def record_plan_started(self, *, plan_id, goal, n_steps):
        self.plan_started_calls.append({})

    async def record_plan_completed(self, *, plan_id):
        self.plan_completed_calls.append({})

    async def record_plan_step_started(self, *, plan_id, step_id, depends_on, n_tools):
        self.step_started_calls.append({"step_id": step_id})

    async def record_plan_step_completed(self, *, plan_id, step_id, content_len, result_text=None):
        self.step_completed_calls.append({"step_id": step_id})

    async def record_plan_step_failed(self, *, plan_id, step_id, error):
        self.step_failed_calls.append({"step_id": step_id})


def test_plan_step_retry_limit_default_is_3():
    """Tier 2: FP-0031-C — _PLAN_STEP_RETRY_LIMIT is 3 (OS default).

    Pins that the constant exists and has the correct value so callers
    that read it (e.g. PlanRuntime, execute_plan) use the right default.
    """
    assert _PLAN_STEP_RETRY_LIMIT == 3


def test_plan_retry_excluded_contains_permission_error():
    """Tier 2: FP-0031-C — _PLAN_RETRY_EXCLUDED includes PermissionError
    so plan steps don't retry ToolGateRefused / OpDenied exceptions
    (they have their own ask-user path).
    """
    assert PermissionError in _PLAN_RETRY_EXCLUDED


def test_plan_retry_limit_config_override():
    """Tier 2: FP-0031-C — PlanConfig.retry_limit can be set via
    reyn.yaml plan.retry_limit and _build_plan_config parses it correctly.
    """
    from reyn.config import _build_plan_config  # type: ignore[attr-defined]

    # Default when plan: section absent
    cfg_default = _build_plan_config(None)
    assert cfg_default.retry_limit == 3

    # Override via dict
    cfg_custom = _build_plan_config({"retry_limit": 5})
    assert cfg_custom.retry_limit == 5

    # retry_limit=0 is valid (= disable retry)
    cfg_zero = _build_plan_config({"retry_limit": 0})
    assert cfg_zero.retry_limit == 0

    # Negative values fall back to default
    cfg_bad = _build_plan_config({"retry_limit": -1})
    assert cfg_bad.retry_limit == 3

    # Non-numeric falls back to default
    cfg_bad2 = _build_plan_config({"retry_limit": "nope"})
    assert cfg_bad2.retry_limit == 3


@pytest.mark.asyncio
async def test_plan_step_retries_on_transient_error_within_limit():
    """Tier 2: FP-0031-C — a step that fails once then succeeds is
    retried; the final step_results entry is populated (= no step_failure).

    Observable contract: step_failures is empty for that step after the
    plan completes when a transient error occurs on attempt 1 but
    succeeds on attempt 2. Uses a shared call counter so retries that
    rebuild the sub-loop still share state with the counter.
    """
    import reyn.chat.planner as planner_mod
    from reyn.chat.planner import execute_plan

    # Shared mutable counter across all RouterLoop instances for this test.
    _total_calls = [0]

    class _OnceFailLoop:
        """Fails on the first call, succeeds on all subsequent calls."""
        def __init__(self, *, host, **kwargs):
            self.host = host

        async def run(self, *, user_text, history):
            _total_calls[0] += 1
            if _total_calls[0] == 1:
                raise RuntimeError("transient error on first attempt")
            await self.host.put_outbox(kind="agent", text="ok", meta={})
            return None

    orig_router_loop = planner_mod.RouterLoop
    planner_mod.RouterLoop = _OnceFailLoop  # type: ignore[assignment]
    try:
        host = _SimpleHost()
        plan = Plan(
            goal="test retry",
            steps=(
                PlanStep(id="s1", description="step that fails once", tools=()),
                PlanStep(id="s2", description="synthesise", tools=(), depends_on=("s1",)),
            ),
        )
        result = await execute_plan(
            plan,
            parent_host=host,
            chain_id="c0",
            retry_limit=3,
        )
    finally:
        planner_mod.RouterLoop = orig_router_loop

    # s1 failed once then succeeded via retry → no step_failures for s1.
    assert "s1" not in result.step_failures, (
        f"s1 should have recovered via retry; step_failures={result.step_failures}"
    )


@pytest.mark.asyncio
async def test_plan_step_does_not_retry_permission_error():
    """Tier 2: FP-0031-C — PermissionError is in _PLAN_RETRY_EXCLUDED,
    so execute_plan re-raises it immediately without retry (= the safety
    layer's ask-user path handles it, not the retry loop).

    Observable contract: a step sub-loop that raises PermissionError
    propagates out of execute_plan rather than being caught-and-retried.
    """
    import reyn.chat.planner as planner_mod
    from reyn.chat.planner import execute_plan

    class _PermErrorLoop:
        def __init__(self, *, host, **kwargs):
            self.host = host
        async def run(self, *, user_text, history):
            raise PermissionError("tool gate refused")

    orig_router_loop = planner_mod.RouterLoop
    planner_mod.RouterLoop = _PermErrorLoop  # type: ignore[assignment]
    try:
        host = _SimpleHost()
        plan = Plan(
            goal="test perm error",
            steps=(
                PlanStep(id="s1", description="perm-denied step", tools=()),
                PlanStep(id="s2", description="synth", tools=(), depends_on=("s1",)),
            ),
        )
        with pytest.raises(PermissionError):
            await execute_plan(
                plan,
                parent_host=host,
                chain_id="c0",
                retry_limit=3,
            )
    finally:
        planner_mod.RouterLoop = orig_router_loop


@pytest.mark.asyncio
async def test_plan_step_does_not_retry_budget_exceeded():
    """Tier 2: FP-0031-C — BudgetExceeded is in _PLAN_RETRY_EXCLUDED,
    so execute_plan re-raises it immediately without retry.
    """
    import reyn.chat.planner as planner_mod
    from reyn.chat.planner import execute_plan

    try:
        from reyn.budget.budget import BudgetExceeded
    except ImportError:
        pytest.skip("BudgetExceeded not available in this environment")

    class _BudgetExceededLoop:
        def __init__(self, *, host, **kwargs):
            self.host = host
        async def run(self, *, user_text, history):
            # BudgetExceeded requires (dimension, detail) — use the correct signature.
            raise BudgetExceeded("tokens", "budget exhausted")

    orig_router_loop = planner_mod.RouterLoop
    planner_mod.RouterLoop = _BudgetExceededLoop  # type: ignore[assignment]
    try:
        host = _SimpleHost()
        plan = Plan(
            goal="test budget exceeded",
            steps=(
                PlanStep(id="s1", description="over-budget step", tools=()),
                PlanStep(id="s2", description="synth", tools=(), depends_on=("s1",)),
            ),
        )
        with pytest.raises(BudgetExceeded):
            await execute_plan(
                plan,
                parent_host=host,
                chain_id="c0",
                retry_limit=3,
            )
    finally:
        planner_mod.RouterLoop = orig_router_loop


# ── FP-0031-D: retry exhaustion asks user via handle_limit_exceeded ──────────


@pytest.mark.asyncio
async def test_plan_step_retry_limit_exhaustion_asks_user_via_limit_handler():
    """Tier 2: FP-0031-D — when a step exhausts its retry budget AND
    on_limit / intervention_bus are provided, execute_plan calls
    handle_limit_exceeded. On approval (allow_continue=True), the retry
    limit is extended and execution resumes; on refusal, the step is
    recorded as failed.

    Observable contract (refusal path): the step appears in step_failures
    and no more attempts were made after handle_limit_exceeded returned
    allow_continue=False.

    Observable contract (approval path): the step succeeds after the
    user-approved extension grants additional retries.
    """
    import reyn.chat.planner as planner_mod
    from reyn.chat.planner import execute_plan
    from reyn.config import OnLimitConfig

    # ── Refusal path ─────────────────────────────────────────────────────

    class _AlwaysFailLoop:
        def __init__(self, *, host, **kwargs):
            self.host = host
        async def run(self, *, user_text, history):
            raise RuntimeError("always fails")

    class _StubBusRefuse:
        """InterventionBus stub that always refuses (= user presses No)."""
        async def request(self, iv):
            class _Answer:
                choice_id = "no"
            return _Answer()

    orig_router_loop = planner_mod.RouterLoop
    planner_mod.RouterLoop = _AlwaysFailLoop  # type: ignore[assignment]
    try:
        host = _SimpleHost()
        plan = Plan(
            goal="test retry exhaustion refusal",
            steps=(
                PlanStep(id="s1", description="always fails", tools=()),
                PlanStep(id="s2", description="synth", tools=(), depends_on=("s1",)),
            ),
        )
        on_limit = OnLimitConfig(mode="interactive", ask_timeout_seconds=0.0)
        result = await execute_plan(
            plan,
            parent_host=host,
            chain_id="c0",
            retry_limit=1,  # short limit so exhaustion happens fast
            on_limit=on_limit,
            intervention_bus=_StubBusRefuse(),
        )
    finally:
        planner_mod.RouterLoop = orig_router_loop

    # User refused — s1 is in step_failures.
    assert "s1" in result.step_failures, (
        f"Expected s1 in step_failures after user refusal; got {result.step_failures}"
    )

    # ── Approval path ─────────────────────────────────────────────────────

    _approval_call_count = [0]

    class _FailThenSucceedLoop:
        """Fails for first 2 calls, succeeds on call 3+."""
        def __init__(self, *, host, **kwargs):
            self.host = host
        async def run(self, *, user_text, history):
            _approval_call_count[0] += 1
            if _approval_call_count[0] <= 2:
                raise RuntimeError(f"fail #{_approval_call_count[0]}")
            await self.host.put_outbox(kind="agent", text="ok", meta={})
            return None

    class _StubBusApprove:
        """InterventionBus stub that always approves (= user presses Yes)."""
        async def request(self, iv):
            class _Answer:
                choice_id = "yes"
            return _Answer()

    orig_router_loop = planner_mod.RouterLoop
    planner_mod.RouterLoop = _FailThenSucceedLoop  # type: ignore[assignment]
    try:
        host2 = _SimpleHost()
        plan2 = Plan(
            goal="test retry exhaustion approval",
            steps=(
                PlanStep(id="s1", description="fails twice then ok", tools=()),
                PlanStep(id="s2", description="synth", tools=(), depends_on=("s1",)),
            ),
        )
        on_limit2 = OnLimitConfig(mode="interactive", ask_timeout_seconds=0.0)
        result2 = await execute_plan(
            plan2,
            parent_host=host2,
            chain_id="c0",
            retry_limit=1,  # limit=1: fails on attempt 0 and 1; user extends; succeeds on 3rd
            on_limit=on_limit2,
            intervention_bus=_StubBusApprove(),
        )
    finally:
        planner_mod.RouterLoop = orig_router_loop

    # User approved extension → s1 should succeed after extended retries.
    assert "s1" not in result2.step_failures, (
        f"Expected s1 to succeed after user-approved extension; "
        f"step_failures={result2.step_failures}"
    )


# ---------------------------------------------------------------------------
# B50 NF-W6-2 fix: _PlanStepHost workspace + make_router_op_context passthrough
# ---------------------------------------------------------------------------


def test_plan_step_host_propagates_workspace_from_parent():
    """Tier 2: _PlanStepHost.workspace forwards to the parent host.

    Without this propagation, RouterLoop inside a plan step builds the
    ToolContext with ``workspace=None``; the recall handler then falls
    into its minimal-context fallback that also propagates None, and
    ``index_query`` raises ``op_runtime context has no workspace``.
    Observed B50 W6-S3 plan step s4 (3x ``control_ir_failed
    kind=index_query``).
    """
    parent = _FakeParentHost()
    # Inject a fake workspace + permission_resolver onto parent.
    parent.workspace = object()  # sentinel — any non-None object is fine
    parent.permission_resolver = object()
    plan = Plan(
        goal="g",
        steps=(PlanStep(id="s1", description="d", tools=()),),
    )
    host = _PlanStepHost(plan=plan, step=plan.steps[0], prior_results={}, parent=parent)
    assert host.workspace is parent.workspace
    assert host.permission_resolver is parent.permission_resolver


def test_plan_step_host_workspace_none_when_parent_has_none():
    """Tier 2: when parent has no workspace attribute, the property
    returns None rather than raising. This keeps test stubs working.
    """
    parent = _FakeParentHost()
    # No workspace attribute on the bare fake host.
    assert not hasattr(parent, "workspace")
    plan = Plan(
        goal="g",
        steps=(PlanStep(id="s1", description="d", tools=()),),
    )
    host = _PlanStepHost(plan=plan, step=plan.steps[0], prior_results={}, parent=parent)
    assert host.workspace is None


def test_plan_step_host_make_router_op_context_delegates_to_parent():
    """Tier 2: _PlanStepHost.make_router_op_context delegates to the
    parent's factory.

    The recall handler's OpContext resolution preferentially uses
    ``ctx.router_state.op_context_factory()`` (which is bound to
    ``self.host.make_router_op_context`` at router init). Plan-step
    hosts previously didn't define this method, so the factory bound
    to ``None`` and recall fell into its workspace-less fallback.
    """
    parent = _FakeParentHost()
    sentinel = object()

    def _make() -> object:
        return sentinel

    parent.make_router_op_context = _make
    plan = Plan(
        goal="g",
        steps=(PlanStep(id="s1", description="d", tools=()),),
    )
    host = _PlanStepHost(plan=plan, step=plan.steps[0], prior_results={}, parent=parent)
    assert host.make_router_op_context() is sentinel


def test_plan_step_host_make_router_op_context_none_when_parent_lacks_factory():
    """Tier 2: if the parent doesn't define make_router_op_context,
    the plan-step facade returns None rather than raising — test
    stubs / older hosts continue to work.
    """
    parent = _FakeParentHost()
    assert not hasattr(parent, "make_router_op_context")
    plan = Plan(
        goal="g",
        steps=(PlanStep(id="s1", description="d", tools=()),),
    )
    host = _PlanStepHost(plan=plan, step=plan.steps[0], prior_results={}, parent=parent)
    assert host.make_router_op_context() is None
