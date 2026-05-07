"""Plan-mode executor — decompose a complex chat query into independent
sub-tasks, run each in a narrow LLM call, aggregate the result.

Origin: post-OSS HN dogfood surfaced "context bloat" — a single chat
router LLM call carrying 7.5KB of system prompt + 14 tools + full
history per turn. For complex queries (= multi-source synthesis,
explain-X-with-code-references), the right shape is decomposition:

    user query → [planner LLM] → [plan artifact]
                                   ↓
                                 [executor: each step in a narrow LLM call]
                                   ↓
                                 [terminal step's text → user]

The planner is just a tool the LLM picks when it sees the query
warrants decomposition. For simple queries (= "hello", single tool
call) the LLM doesn't call ``plan`` and the existing path runs
unchanged. Plan-mode is therefore opt-in **per query**, by the LLM,
based on the ``plan`` tool's description.

Architecture choice (= per the design doc):

  - **No new LLM call site.** Plan steps run inside ``RouterLoop``
    (= existing chat-side LLM caller) with a ``_PlanStepHost`` facade
    that narrows the tool catalogue to the step's allowed tools and
    swaps the system prompt for a step-specific template. The phase
    LLM caller is untouched; we don't introduce a third caller.
  - **No skill abstraction reuse for MVP.** Plan is transient (= one
    chat turn), not persistable. Lifting plan onto skills (= plan as
    stdlib skill, persisted via skill_resume infra) is a later
    migration when resume / replay become hard requirements.
  - **In-memory plan artifact.** No workspace persistence in MVP.
    Plan steps emit ``plan_*`` events for audit (= P6).

P7-clean: this module contains no skill-specific strings. Step names
and descriptions come entirely from the LLM-emitted plan; the OS only
validates structure.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from reyn.chat.router_loop import RouterLoop, RouterLoopHost
from reyn.llm.pricing import TokenUsage


# Each step's narrow LLM call gets at most this many iterations before
# the OS gives up on it and records a step failure. Smaller than the
# top-level router loop's default (= 5) because plan steps are scoped:
# tool_call → narrate is the natural shape, two iterations covers it
# with a budget buffer.
_PLAN_STEP_MAX_ITERATIONS = 3

# Plan-tool argument bounds. Pinned in the JSON schema (= router_tools)
# AND re-validated here so a malformed plan from the LLM is rejected
# with a structured error instead of crashing the executor.
_PLAN_MIN_STEPS = 2
_PLAN_MAX_STEPS = 7


# ── Data model ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlanStep:
    """One unit of work in a plan."""
    id: str
    description: str
    tools: tuple[str, ...]
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    """LLM-emitted decomposition of a complex query.

    ``steps`` is in the order the LLM emitted them. The executor performs
    its own topological sort (= ``_topological_order``) so the LLM's
    ordering doesn't have to match the dependency graph.
    """
    goal: str
    steps: tuple[PlanStep, ...]


@dataclass
class PlanExecutionResult:
    """Output of ``execute_plan``. ``text`` is what the user sees; the
    per-step results are kept for audit / debugging.
    """
    text: str
    step_results: dict[str, str] = field(default_factory=dict)
    step_failures: dict[str, str] = field(default_factory=dict)
    usage: TokenUsage = field(default_factory=TokenUsage)


class PlanValidationError(ValueError):
    """Raised when an LLM-emitted plan fails structural validation.

    The executor catches this at the dispatch boundary and surfaces a
    JSON-RPC-style error result back to the LLM so it can correct.
    """


# ── Parsing + validation ────────────────────────────────────────────────────


def parse_and_validate_plan(args: dict, *, allowed_tool_names: set[str]) -> Plan:
    """Convert raw plan tool-call arguments into a typed ``Plan``.

    ``allowed_tool_names`` is the set of router-tool names available in
    the current chat context (= what ``RouterLoop._catalog`` would see).
    Any step that lists a tool outside this set fails validation —
    plans can only invoke tools the OS already exposes; they cannot
    invent new tools.

    Raises :class:`PlanValidationError` for any structural defect:

      - missing / empty goal
      - step count outside ``[_PLAN_MIN_STEPS, _PLAN_MAX_STEPS]``
      - duplicate step ids
      - empty / non-string fields
      - tool names outside ``allowed_tool_names``
      - unknown ``depends_on`` references
      - cycles in the dependency graph

    The executor is responsible for catching ``PlanValidationError``
    and surfacing a structured error to the LLM (= so the LLM can re-
    emit a corrected plan rather than the OS crashing).
    """
    if not isinstance(args, dict):
        raise PlanValidationError(f"plan args must be an object, got {type(args).__name__}")

    goal = args.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise PlanValidationError("plan.goal must be a non-empty string")

    # steps is wire-encoded as a JSON string (= Gemini-safe schema budget;
    # see router_tools.py:G1). Parse + validate here. Accept either the
    # ``steps_json`` field (= current schema) or a legacy ``steps`` field
    # (= forward-compat hatch in case the LLM emits the typed array form).
    raw_steps_json = args.get("steps_json")
    raw_steps_typed = args.get("steps")
    if isinstance(raw_steps_json, str):
        try:
            raw_steps = json.loads(raw_steps_json)
        except json.JSONDecodeError as exc:
            raise PlanValidationError(
                f"plan.steps_json is not valid JSON: {exc}"
            ) from exc
    elif isinstance(raw_steps_typed, list):
        raw_steps = raw_steps_typed
    else:
        raise PlanValidationError(
            "plan.steps_json must be a JSON-encoded string of an array "
            "of step objects (or 'steps' may be a list directly)"
        )
    if not isinstance(raw_steps, list):
        raise PlanValidationError(
            f"plan steps must decode to a list, got {type(raw_steps).__name__}"
        )
    if not (_PLAN_MIN_STEPS <= len(raw_steps) <= _PLAN_MAX_STEPS):
        raise PlanValidationError(
            f"plan.steps must contain between {_PLAN_MIN_STEPS} and "
            f"{_PLAN_MAX_STEPS} steps; got {len(raw_steps)}. "
            "(For simpler queries, reply directly or call a single tool "
            "instead of using plan.)"
        )

    steps: list[PlanStep] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise PlanValidationError(
                f"plan.steps[{i}] must be an object, got {type(raw).__name__}"
            )
        sid = raw.get("id")
        if not isinstance(sid, str) or not sid.strip():
            raise PlanValidationError(f"plan.steps[{i}].id must be a non-empty string")
        if sid in seen_ids:
            raise PlanValidationError(f"plan.steps duplicate id: {sid!r}")
        seen_ids.add(sid)
        desc = raw.get("description")
        if not isinstance(desc, str) or not desc.strip():
            raise PlanValidationError(
                f"plan.steps[{i}] (id={sid!r}).description must be a non-empty string"
            )
        raw_tools = raw.get("tools", [])
        if not isinstance(raw_tools, list):
            raise PlanValidationError(
                f"plan.steps[{i}] (id={sid!r}).tools must be a list (use [] for narration-only steps)"
            )
        tools_tuple: tuple[str, ...] = tuple(raw_tools)
        for t in tools_tuple:
            if not isinstance(t, str):
                raise PlanValidationError(
                    f"plan.steps[{i}] (id={sid!r}).tools[*] must be strings"
                )
            if t not in allowed_tool_names:
                raise PlanValidationError(
                    f"plan.steps[{i}] (id={sid!r}).tools references {t!r} "
                    f"which is not in the available tool catalog. "
                    f"Allowed: {sorted(allowed_tool_names)}"
                )
        raw_deps = raw.get("depends_on", [])
        if not isinstance(raw_deps, list):
            raise PlanValidationError(
                f"plan.steps[{i}] (id={sid!r}).depends_on must be a list"
            )
        deps_tuple: tuple[str, ...] = tuple(raw_deps)
        for d in deps_tuple:
            if not isinstance(d, str):
                raise PlanValidationError(
                    f"plan.steps[{i}] (id={sid!r}).depends_on[*] must be strings"
                )

        steps.append(PlanStep(id=sid, description=desc, tools=tools_tuple, depends_on=deps_tuple))

    # Validate depends_on reference integrity AFTER all step ids are collected
    # (= forward references are allowed — the LLM may emit them in any order
    # and the executor topo-sorts).
    for step in steps:
        for d in step.depends_on:
            if d not in seen_ids:
                raise PlanValidationError(
                    f"plan.steps[id={step.id!r}].depends_on references unknown id {d!r}"
                )

    # Cycle detection via a topological sort dry-run.
    try:
        _topological_order(steps)
    except PlanValidationError:
        # Re-raise as-is; the function's error message already explains.
        raise

    return Plan(goal=goal.strip(), steps=tuple(steps))


def _topological_order(steps: list[PlanStep] | tuple[PlanStep, ...]) -> list[PlanStep]:
    """Kahn's algorithm. Raises ``PlanValidationError`` on cycle.

    Stable: among nodes with equal in-degree, preserves the LLM-emitted
    order. The chat router doesn't need parallelism — sequential
    execution makes the per-step events log readable as a normal turn-
    by-turn replay.
    """
    by_id = {s.id: s for s in steps}
    indeg = {s.id: len(s.depends_on) for s in steps}
    ready = [s for s in steps if indeg[s.id] == 0]
    out: list[PlanStep] = []
    while ready:
        current = ready.pop(0)
        out.append(current)
        for s in steps:
            if current.id in s.depends_on:
                indeg[s.id] -= 1
                if indeg[s.id] == 0:
                    ready.append(by_id[s.id])
    if len(out) != len(steps):
        unresolved = [s.id for s in steps if s not in out]
        raise PlanValidationError(
            f"plan.steps contains a dependency cycle. Unresolvable: {unresolved}"
        )
    return out


# ── Step system prompt ──────────────────────────────────────────────────────


def build_plan_step_system_prompt(plan: Plan, step: PlanStep, prior_results: dict[str, str]) -> str:
    """Construct the narrow system prompt for one plan step.

    Distinct from the full chat router prompt: drops Identity / Project /
    Behaviour / ROUTING-RULE sections (= those are about routing across
    the full intent axis; a plan step has a single fixed assignment).
    Keeps a 1-paragraph step framing, the goal, the assignment, and
    prior step outputs as context.

    Sized to be ~500-1500 chars vs. the full prompt's ~7500 chars (=
    direct mitigation of the per-call context bloat the dogfood
    surfaced). Each plan step is a focused LLM call, not a general-
    purpose router invocation.
    """
    parts: list[str] = []
    parts.append(
        "You are a Reyn agent executing one step of a multi-step plan. "
        "Use the tools provided (if any) to gather information, then "
        "emit a concise text reply (100-400 chars) summarising what "
        "this step contributes to the plan goal. Do NOT restate the "
        "full plan; focus on this step's output."
    )
    parts.append("")
    parts.append(f"## Plan goal\n{plan.goal}")
    parts.append("")
    parts.append(f"## This step (id={step.id})\n{step.description}")
    if step.depends_on:
        parts.append("")
        parts.append("## Prior step results (your inputs)")
        for dep in step.depends_on:
            result = prior_results.get(dep, "(no result)")
            parts.append(f"### {dep}\n{result}")
    return "\n".join(parts)


# ── Narrow host facade ──────────────────────────────────────────────────────

# Tool families that need their respective host data plumbed through.
# Used by ``_PlanStepHost`` to decide whether a given host method should
# return narrow data or be silenced (= return empty / None).
_FILE_TOOL_NAMES = frozenset({"list_directory", "read_file", "write_file", "delete_file"})
_MCP_TOOL_NAMES = frozenset({"list_mcp_servers", "list_mcp_tools", "call_mcp_tool"})
_WEB_FETCH_TOOL_NAME = "web_fetch"
_INVOKE_SKILL_TOOL_NAME = "invoke_skill"
_DELEGATE_TOOL_NAME = "delegate_to_agent"


class _PlanStepHost:
    """RouterLoopHost facade narrowing scope to one plan step.

    Every method either passes through to the parent host (for tool
    dispatch) or narrows what RouterLoop sees when building the catalog
    (for ``list_*`` / ``get_*`` introspection methods). The narrowing
    is what makes per-step LLM calls small: if a step's tools doesn't
    include ``list_skills``, the parent's 25-skill list never reaches
    the step's system prompt or tool schema.
    """

    def __init__(
        self,
        *,
        plan: Plan,
        step: PlanStep,
        prior_results: dict[str, str],
        parent: RouterLoopHost,
    ):
        self._plan = plan
        self._step = step
        self._prior_results = prior_results
        self._parent = parent
        self._tool_set: frozenset[str] = frozenset(step.tools)
        # Captured by put_outbox; the executor reads this after RouterLoop
        # finishes to collect this step's text contribution.
        self._captured_text: str = ""

    # ── RouterLoopHost-required attributes (= identity / static config) ────

    @property
    def chat_id(self) -> str:
        return getattr(self._parent, "chat_id", "")

    @property
    def agent_name(self) -> str:
        return getattr(self._parent, "agent_name", "")

    @property
    def agent_role(self) -> str:
        # The narrow system prompt overrides this anyway (via
        # system_prompt_override on RouterLoop), but build_system_prompt
        # still reads agent_role unconditionally during catalog construction
        # in some code paths. Keep parent's role for safety.
        return getattr(self._parent, "agent_role", "")

    @property
    def output_language(self) -> str | None:
        return getattr(self._parent, "output_language", None)

    @property
    def events(self) -> Any:
        return self._parent.events

    # ── Catalog narrowing — what tools / skills / agents are visible ──────

    def list_available_skills(self) -> list[dict]:
        # Skills only visible if the step asked for invoke_skill / describe_skill.
        if _INVOKE_SKILL_TOOL_NAME in self._tool_set or "describe_skill" in self._tool_set:
            return self._parent.list_available_skills()
        return []

    def list_available_agents(self) -> list[dict]:
        if _DELEGATE_TOOL_NAME in self._tool_set or "describe_agent" in self._tool_set:
            return self._parent.list_available_agents()
        return []

    def get_memory_index(self) -> dict:
        if "list_memory" in self._tool_set or "read_memory_body" in self._tool_set:
            return self._parent.get_memory_index()
        return {"status": "not_found", "content": ""}

    def get_file_permissions(self) -> dict | None:
        if self._tool_set & _FILE_TOOL_NAMES:
            return self._parent.get_file_permissions()
        return None

    def get_mcp_servers(self) -> list[dict]:
        if self._tool_set & _MCP_TOOL_NAMES:
            return self._parent.get_mcp_servers()
        return []

    def get_web_fetch_allowed(self) -> bool:
        if _WEB_FETCH_TOOL_NAME in self._tool_set:
            return self._parent.get_web_fetch_allowed()
        return False

    def get_project_context(self) -> str:
        # Project context narrowed out by default — plan steps work from
        # the step description, not from project-wide background.
        return ""

    # ── Memory file paths (kept for read_memory_body / remember_*) ────────

    def memory_path(self, layer: str, slug: str) -> str:
        return self._parent.memory_path(layer, slug)

    def memory_dir(self, layer: str) -> str:
        return self._parent.memory_dir(layer)

    # ── Tool dispatch (= passthrough to parent) ───────────────────────────

    async def web_search(self, *, query: str, max_results: int) -> dict:
        return await self._parent.web_search(query=query, max_results=max_results)

    async def web_fetch(self, *, url: str, max_length: int) -> dict:
        return await self._parent.web_fetch(url=url, max_length=max_length)

    async def reyn_src_list(self, *, path: str) -> dict:
        return await self._parent.reyn_src_list(path=path)

    async def reyn_src_read(self, *, path: str) -> dict:
        return await self._parent.reyn_src_read(path=path)

    async def file_read(self, path: str) -> str:
        return await self._parent.file_read(path)

    async def file_write(self, path: str, content: str) -> dict:
        return await self._parent.file_write(path, content)

    async def file_regenerate_index(self, *args, **kwargs) -> dict:
        return await self._parent.file_regenerate_index(*args, **kwargs)

    async def file_list_directory(self, path: str) -> list[dict]:
        return await self._parent.file_list_directory(path)

    async def run_skill_awaitable(self, *, skill: str, input: dict, chain_id: str) -> dict:
        # Plan steps may run skills if invoke_skill is in step.tools.
        # Lifecycle: we don't allow nested plans (= a skill spawning
        # another plan would create unbounded recursion). The skill
        # itself can use Control IR / preprocessor as usual.
        return await self._parent.run_skill_awaitable(
            skill=skill, input=input, chain_id=chain_id,
        )

    async def send_to_agent(self, *, to: str, request: str, depth: int, chain_id: str) -> None:
        return await self._parent.send_to_agent(
            to=to, request=request, depth=depth, chain_id=chain_id,
        )

    async def put_outbox(self, *, kind: str, text: str, meta: dict) -> None:
        # CAPTURE this step's text instead of forwarding to the user's
        # outbox — the user sees only the aggregator's output (= the
        # terminal step's reply or an explicit synthesis step), not
        # intermediate per-step replies. Tool-error / status messages
        # also stay confined to the step.
        if kind == "agent" and text:
            self._captured_text = text
        # Other kinds (= status / trace) are dropped silently for the
        # plan step — they don't survive into the user-facing reply.

    @property
    def captured_text(self) -> str:
        return self._captured_text


# ── Executor ────────────────────────────────────────────────────────────────


async def execute_plan(
    plan: Plan,
    *,
    parent_host: RouterLoopHost,
    chain_id: str,
    budget: Any = None,
    router_model: str = "light",
) -> PlanExecutionResult:
    """Run a plan step-by-step in topological order, return the aggregated text.

    Each step runs through ``RouterLoop`` with a ``_PlanStepHost``
    facade. Per-step LLM calls have a narrow system prompt and a
    subset of the parent's tool catalog. Step outputs are collected
    into ``prior_results`` and made visible to dependent steps via
    the next step's system prompt.

    The "aggregator" is the **terminal step** in topological order:
    its captured text is the user-facing reply. Plans without an
    explicit synthesis step return the last step's text (= still works,
    just less curated).
    """
    parent_host.events.emit(
        "plan_emitted",
        chain_id=chain_id,
        goal=plan.goal,
        n_steps=len(plan.steps),
        step_ids=[s.id for s in plan.steps],
    )

    ordered = _topological_order(plan.steps)
    step_results: dict[str, str] = {}
    step_failures: dict[str, str] = {}
    total_usage = TokenUsage()

    for step in ordered:
        parent_host.events.emit(
            "plan_step_started",
            chain_id=chain_id,
            step_id=step.id,
            depends_on=list(step.depends_on),
            n_tools=len(step.tools),
        )
        narrow_host = _PlanStepHost(
            plan=plan, step=step, prior_results=step_results, parent=parent_host,
        )
        sys_prompt = build_plan_step_system_prompt(plan, step, step_results)
        sub_loop = RouterLoop(
            host=narrow_host,
            chain_id=chain_id,
            max_iterations=_PLAN_STEP_MAX_ITERATIONS,
            router_model=router_model,
            budget=budget,
            system_prompt_override=sys_prompt,
        )
        try:
            sub_usage = await sub_loop.run(
                user_text=step.description, history=[],
            )
            if sub_usage is not None:
                total_usage.prompt_tokens += sub_usage.prompt_tokens
                total_usage.completion_tokens += sub_usage.completion_tokens
        except Exception as exc:  # noqa: BLE001 — defensive, don't crash plan
            step_failures[step.id] = repr(exc)
            parent_host.events.emit(
                "plan_step_failed",
                chain_id=chain_id,
                step_id=step.id,
                error=repr(exc),
            )
            continue

        text = narrow_host.captured_text
        step_results[step.id] = text
        parent_host.events.emit(
            "plan_step_completed",
            chain_id=chain_id,
            step_id=step.id,
            content_len=len(text),
        )

    # Aggregator = the topologically-last step whose result is non-empty.
    # If all steps failed, surface a synthesised error instead of empty.
    final_text = ""
    for step in reversed(ordered):
        if step.id in step_results and step_results[step.id]:
            final_text = step_results[step.id]
            break
    if not final_text:
        final_text = (
            f"Plan execution produced no aggregate reply. "
            f"{len(step_failures)} of {len(ordered)} steps failed."
        )

    parent_host.events.emit(
        "plan_aggregated",
        chain_id=chain_id,
        n_completed=len(step_results),
        n_failed=len(step_failures),
        result_len=len(final_text),
    )

    return PlanExecutionResult(
        text=final_text,
        step_results=step_results,
        step_failures=step_failures,
        usage=total_usage,
    )


# ── Plan tool dispatch entry point ──────────────────────────────────────────


async def dispatch_plan_tool(
    *,
    args: dict,
    parent_host: RouterLoopHost,
    chain_id: str,
    budget: Any = None,
    router_model: str = "light",
    available_tool_names: set[str],
) -> dict:
    """Entry point invoked from the chat router's ``plan`` tool dispatch.

    Returns a dict in the shape RouterLoop expects from any tool
    handler: ``{status, ...}``. On success the ``text`` field carries
    the aggregated user-facing reply.

    The chat router_loop dispatcher will treat this differently from a
    regular tool: instead of round-tripping the result back into the
    LLM for narration, it forwards ``text`` directly to the user
    outbox. This avoids a redundant aggregator LLM call on top of the
    plan's own terminal step.
    """
    try:
        plan = parse_and_validate_plan(args, allowed_tool_names=available_tool_names)
    except PlanValidationError as exc:
        return {
            "status": "error",
            "error": {"kind": "plan_invalid", "message": str(exc)},
        }
    result = await execute_plan(
        plan,
        parent_host=parent_host,
        chain_id=chain_id,
        budget=budget,
        router_model=router_model,
    )
    return {
        "status": "ok",
        "text": result.text,
        "step_results": result.step_results,
        "step_failures": result.step_failures,
        "n_steps": len(plan.steps),
    }


__all__ = [
    "Plan",
    "PlanStep",
    "PlanExecutionResult",
    "PlanValidationError",
    "build_plan_step_system_prompt",
    "dispatch_plan_tool",
    "execute_plan",
    "parse_and_validate_plan",
]
