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
  - **Plan not on the skill abstraction.** Plan does not reuse the skill
    machinery; lifting plan onto skills (= plan as stdlib skill) remains a
    possible later migration.
  - **Plan artifact is workspace-persisted (since ADR-0023 Phase 2).** The
    original MVP kept the plan in-memory only; ADR-0023 promoted it to a
    workspace-persisted decomposition (``write_plan_decomposition``, P5 SSoT)
    with crash-resume via ``PlanSnapshot`` + ``plan_*`` WAL / audit events (P6).

P7-clean: this module contains no skill-specific strings. Step names
and descriptions come entirely from the LLM-emitted plan; the OS only
validates structure.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reyn.chat.router_loop import (
    EMPTY_STOP_RETRY_DIRECTIVE,
    RouterLoop,
    RouterLoopHost,
)
from reyn.llm.pricing import TokenUsage

if TYPE_CHECKING:
    from reyn.config import PlannerStepCompactionConfig
    from reyn.services.compaction.engine import CompactionEngine

logger = logging.getLogger(__name__)


# WorkflowAbortedError is raised by skills via DSL `abort` semantics; if a
# plan step's invoked skill aborts cleanly, we treat the plan itself as
# completed normally (= same posture as ADR-0013 runtime crash lifecycle).
# Imported lazily inside the finally clause to avoid circular imports.
def _is_workflow_abort(exc_type: type | None) -> bool:
    if exc_type is None:
        return False
    try:
        from reyn.kernel.runtime import WorkflowAbortedError
        return issubclass(exc_type, WorkflowAbortedError)
    except ImportError:
        return False


# Each step's narrow LLM call gets at most this many iterations before
# the OS gives up on it and records a step failure. Raised from 3 to 5
# in FP-0029: tool_call → result → follow-up tool → narrate is a
# realistic 3-turn shape; 5 gives comfortable headroom without runaway
# risk. Overridable via ``reyn.yaml plan.step_max_iterations``.
_PLAN_STEP_MAX_ITERATIONS = 5

# Plan-tool argument bounds. Pinned in the JSON schema (= router_tools)
# AND re-validated here so a malformed plan from the LLM is rejected
# with a structured error instead of crashing the executor.
_PLAN_MIN_STEPS = 2
_PLAN_MAX_STEPS = 7

# FP-0031-C: auto-retry on transient step failures.
# Maximum retries per step before the OS asks the user for an extension
# (Component D) or records a step failure and continues.
# Overridable via ``reyn.yaml plan.retry_limit``.
_PLAN_STEP_RETRY_LIMIT = 3

# #1285 PR2 (#1092 plan-axis force-close re-entry): defensive cap on force-close
# re-entries per step. by-construction termination (PR1 assert_turn_budget_bounds:
# progress_margin > 0 ⇒ each re-entry makes progress) already guarantees finite
# re-entries; this is a generous backstop — on cap the step terminates best-effort
# at the last consolidation (the PR1 FLOOR). DISTINCT from _PLAN_STEP_RETRY_LIMIT
# (FP-0031 transient-failure retry) — the two counters never conflate.
_PLAN_STEP_MAX_FORCE_CLOSE_REENTRIES = 8

# Continuation framing for a force-close re-entry: the bounded wrap-up
# consolidation is handed back as the next user turn so the re-entered step
# CONTINUES from it (not a restart). run() builds [system(goal), user(this)];
# {consolidation} is filled per re-entry.
_PLAN_STEP_FORCE_CLOSE_CONTINUE_TEMPLATE = (
    "The work on this step so far has been consolidated below (the working "
    "context reached its size limit and was wrapped up). Continue from this "
    "consolidation toward completing the step — do NOT restart from scratch.\n\n"
    "--- Consolidation of work so far ---\n{consolidation}"
)

# B42-NF-W6-1 / #187: the plan-step empty-stop continuation directive is now the
# SHARED uniform ``EMPTY_STOP_RETRY_DIRECTIVE`` ("resume") imported from
# router_loop.py (see its definition for the owner no-per-site-differentiation
# decision). The old plan-step "write your step report / do not call another
# tool" directive was retired (unevidenced differentiation + anti-invoke framing).


# B51 NF-W6-3 fix: directive template for the plan_invalid self-correction
# loop. The router LLM gets the parser error message back inside a
# user-role directive and is asked to re-emit a corrected plan call.
#
# Observed failure: weak-tier LLM (= flash-lite) generates a plan tool
# call whose ``steps_json`` includes the user's quoted phrase verbatim
# inside a step description, forgetting that the JSON-encoded outer
# string requires inner ``"`` to be backslash-escaped. The JSON parser
# fails (e.g. "Expecting ',' delimiter at char 445") and the plan
# never spawns.
#
# Mitigation: the router loop intercepts the plan_invalid tool result,
# formats this directive with the (sanitised) parser error, appends it
# as a user-role message, and re-enters the LLM loop bounded by
# ``safety.loop.plan_invalid_retries`` (= dedicated counter). The
# closing line guards against a meta-loop where the LLM copies the
# error text into the new steps_json and re-fails.
_PLAN_INVALID_RETRY_DIRECTIVE_TEMPLATE = (
    "Your previous plan() call failed validation: {error_message}\n"
    "Common cause: unescaped \" inside step description / id / tools "
    "strings. Re-emit the plan with all inner \" escaped as \\\". "
    "Do not copy the original error text into the new steps_json."
)


def _build_plan_invalid_retry_directive(error_message: str) -> str:
    """Render the plan_invalid retry directive with a sanitised error message.

    Strips control characters (= LLM-injection safety: an attacker
    crafting a steps_json that triggers a parser error embedding control
    bytes should not be able to smuggle them into the retry prompt) and
    caps the length so the directive stays bounded.
    """
    safe = re.sub(r"[\x00-\x1f\x7f]", "", error_message or "")[:500]
    return _PLAN_INVALID_RETRY_DIRECTIVE_TEMPLATE.format(error_message=safe)

# Exception types that must NOT be retried and must be re-raised to their
# own safety-layer ask/abort path. Retrying them would cause double-ask or
# bypass budget enforcement invariants.
#   PermissionError          — ToolGateRefused / OpDenied: wait for user approval
#   BudgetExceeded           — cost guard: abort / ask in BudgetGateway
#   PhaseBudgetExceededError — phase-level token cap: handled by OS runtime
#   LoopLimitExceededError   — loop guard: handled by handle_limit_exceeded
#
# WorkflowAbortedError is NOT in this tuple: it is a deliberate step-level
# termination that should be recorded as a step failure and let the plan
# continue (per the ADR-0013 pattern pinned in test_plan_lifecycle_crash.py).
# KeyboardInterrupt / SystemExit / asyncio.CancelledError are handled as
# system signals in a separate except clause below.
def _build_retry_excluded() -> tuple:
    """Build the tuple of exception classes to exclude from retry at import time.

    Falls back gracefully if any class is unavailable (= test environments).
    """
    classes: list[type] = [PermissionError]
    try:
        from reyn.budget.budget import BudgetExceeded
        classes.append(BudgetExceeded)
    except ImportError:
        pass
    try:
        from reyn.kernel.runtime_types import (
            LoopLimitExceededError,
            PhaseBudgetExceededError,
        )
        classes.extend([PhaseBudgetExceededError, LoopLimitExceededError])
    except ImportError:
        pass
    return tuple(classes)

_PLAN_RETRY_EXCLUDED: tuple = _build_retry_excluded()


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

    ``plan_goal`` and ``n_steps`` are populated by ``execute_plan`` so
    callers (= FP-0025 C: ``spawn_plan_task``) can enqueue a
    ``plan_completed`` inbox message without re-reading the plan object.
    """
    text: str
    step_results: dict[str, str] = field(default_factory=dict)
    step_failures: dict[str, str] = field(default_factory=dict)
    usage: TokenUsage = field(default_factory=TokenUsage)
    plan_goal: str = ""
    n_steps: int = 0


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


def build_plan_step_system_prompt(
    plan: Plan,
    step: PlanStep,
    prior_results: dict[str, str],
    *,
    output_language: str | None = None,
    cwd: str | None = None,
) -> str:
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

    ``output_language``: when set, prepends a language directive so the
    step LLM replies in the user's language (= Component A fix for the
    JA-user bug where plan step LLMs ignored the session output_language).

    ``cwd``: project root / working directory to inject so step LLMs can
    anchor relative file paths correctly (= B28-MED-3 fix). When None,
    ``Path.cwd()`` is used at call time.
    """
    effective_cwd = cwd if cwd is not None else str(Path.cwd())
    parts: list[str] = []
    if output_language:
        parts.append(f"Respond in {output_language}.")
        parts.append("")
    parts.append(f"You are at project root: {effective_cwd}")
    parts.append("")
    parts.append(
        "You are a Reyn agent executing one step of a multi-step plan. "
        "Use the tools provided (if any) to gather information, then "
        "Report what this step found. Include concrete details: code snippets, "
        "function signatures, specific line numbers, exact values, structured data. "
        "Aim for ~800 characters as a soft target; exceed if the content requires "
        "it (e.g. multi-line code blocks). Be factual — a separate synthesis step "
        "will produce the user reply."
    )
    parts.append("")
    parts.append(f"## Plan goal\n{plan.goal}")
    parts.append("")
    parts.append(f"## Your task\n{step.description}")
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
_MCP_TOOL_NAMES = frozenset({"list_mcp_servers", "list_mcp_tools", "call_mcp_tool", "describe_mcp_tool"})
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
        turn_budget_engine: Any = None,
    ):
        self._plan = plan
        self._step = step
        self._prior_results = prior_results
        self._parent = parent
        self._tool_set: frozenset[str] = frozenset(step.tools)
        # Captured by put_outbox; the executor reads this after RouterLoop
        # finishes to collect this step's text contribution.
        self._captured_text: str = ""
        # #1285 (#1092 plan-axis force-close, PR1): the cumulative-current-turn
        # TurnBudgetEngine for this step. None → should_force_close is inert
        # (byte-identical to pre-#1285). A plan step is goal-bearing + autonomous
        # (no user mid-step), so it mirrors the PHASE axis (PROACTIVE force-close),
        # not chat's REACTIVE handoff.
        self._turn_budget_engine = turn_budget_engine
        # Set by record_force_close when run_loop fires the wrap-up; the planner
        # reads forced_close_result after the step's run() to use the bounded
        # consolidation as the step's output (PR1 FLOOR; PR2 re-enters from it).
        self._forced_close_result: Any = None

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

    @property
    def resolver(self) -> Any:
        # #1172: delegate to parent so a nested CompactionEngine resolves
        # model classes through the same chain.
        return self._parent.resolver

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
        # FP-0022: web_fetch is always allowed at the catalog level; authorization
        # is enforced at the handler level. Return True when the step's tool_set
        # includes web_fetch, matching the parent's always-True behavior.
        return _WEB_FETCH_TOOL_NAME in self._tool_set

    def get_project_context(self) -> str:
        # Project context narrowed out by default — plan steps work from
        # the step description, not from project-wide background.
        return ""

    # ── Cumulative-axis force-close (#1285 / #1092 plan axis, PR1 FLOOR) ───
    # Mirrors the PHASE host (PhaseRouterLoopHost) force-close interface: a
    # plan step is goal-bearing + autonomous (no user mid-step), so it uses
    # PROACTIVE force-close (not chat's REACTIVE handoff). When the engine is
    # absent (not activated), should_force_close is always False = byte-
    # identical to pre-#1285. ORTHOGONAL to FP-0031-C/D: force-close is the
    # *cumulative-context* trigger; FP-0031 is the *transient-failure* trigger.

    async def should_force_close(self, messages: list[dict], *, model: str) -> bool:
        """Layer-1 force-close trigger for the plan axis. RouterLoop.run_loop
        consults this each turn AFTER compaction; True when the accumulated
        current-turn content (non-system turns; the wrap-up SP swaps the system
        turn at force-close time) has reached the TurnBudgetEngine threshold.
        Engine absent → always False (inert)."""
        engine = self._turn_budget_engine
        if engine is None:
            return False
        from reyn.services.compaction.engine import estimate_tokens_for_turn

        content_tokens = sum(
            estimate_tokens_for_turn(m, model, use_chars4=True)
            for m in messages
            if isinstance(m, dict) and m.get("role") != "system"
        )
        return engine.should_force_close(content_tokens)

    def record_force_close(self, result: Any) -> None:
        """RouterLoop.run_loop hands the force-close consolidation finish here;
        the planner reads ``forced_close_result`` after the step's run() to use
        the bounded consolidation as the step output (PR1 FLOOR). Stored, not
        acted on (P3 — the planner drives the handoff)."""
        self._forced_close_result = result

    @property
    def forced_close_result(self) -> Any:
        """The consolidation finish of a force-close in the last run, or None."""
        return self._forced_close_result

    @property
    def wrap_up_output_reserve(self) -> int | None:
        """The wrap-up call's OUTPUT budget (``output_reserve``), or None when no
        engine. RouterLoop._force_close_call passes it as ``max_tokens`` to
        HARD-CAP the consolidation ≤ output_reserve — the by-construction
        guarantee (assert_turn_budget_bounds: output_reserve + offload_cap <
        threshold)."""
        engine = self._turn_budget_engine
        return engine.budget.output_reserve if engine is not None else None

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

    async def file_delete(self, path: str) -> dict:
        return await self._parent.file_delete(path)

    async def file_regenerate_index(self, *args, **kwargs) -> dict:
        return await self._parent.file_regenerate_index(*args, **kwargs)

    async def file_list_directory(self, path: str) -> list[dict]:
        return await self._parent.file_list_directory(path)

    # ── MCP passthroughs (= delegate to parent) ───────────────────────────

    async def mcp_list_servers(self) -> list[dict]:
        return await self._parent.mcp_list_servers()

    async def mcp_list_tools(self, server: str) -> list[dict]:
        return await self._parent.mcp_list_tools(server)

    async def mcp_call_tool(self, server: str, tool: str, args: dict) -> dict:
        return await self._parent.mcp_call_tool(server, tool, args)

    # ── Model resolution (= required by RouterLoop for LLM call) ──────────
    #
    # 2026-05-07 dogfood bug fix: this method was missing from the original
    # _PlanStepHost design (commit 6b41fd0). Without it, RouterLoop.run()
    # raises AttributeError when computing the model spec, so every plan
    # step fails. Discovered when "Read both README.md and CLAUDE.md, then
    # build a comparison" produced 3-of-3 step_failures. Delegate to parent.
    def resolve_model(self, name: str) -> str:
        return self._parent.resolve_model(name)

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

    # ── Workspace / op_context passthrough (B50 NF-W6-2 fix) ──────────────
    #
    # _PlanStepHost previously omitted ``workspace`` and
    # ``make_router_op_context`` from the parent passthrough surface.
    # Consequence: when a plan step's LLM called a router-side tool
    # whose handler builds an OpContext (e.g. ``recall``, which dispatches
    # ``index_query`` via op_runtime), RouterLoop built the ToolContext
    # with ``workspace=getattr(self.host, "workspace", None)`` → None,
    # and the recall handler fell into its minimal-context fallback that
    # also propagates None, so ``index_query`` raised
    # ``op_runtime context has no workspace``. Observed B50 W6-S3 plan
    # step s4 (3x ``control_ir_failed kind=index_query``).
    #
    # Pass through both surfaces to the parent so plan-step tool calls
    # see the same workspace + OpContext factory the chat router uses.
    # The narrowing this facade provides is at the catalog / tool-set
    # layer (= what tools the step can see); workspace itself is a
    # global property of the agent and must not be narrowed.

    @property
    def workspace(self) -> Any:
        return getattr(self._parent, "workspace", None)

    @property
    def permission_resolver(self) -> Any:
        return getattr(self._parent, "permission_resolver", None)

    def make_router_op_context(self) -> Any:
        factory = getattr(self._parent, "make_router_op_context", None)
        if factory is None:
            return None
        return factory()


# ── Executor ────────────────────────────────────────────────────────────────


# ── Resume classification helpers (ADR-0023 §3.4) ──────────────────────────


def _build_resume_classifier(resume_plan: Any) -> Any:
    """Return a callable ``step_id → Literal["memo", "memo_failed", "execute"]``.

    When ``resume_plan`` is None (= fresh run), every step classifies
    as ``"execute"``. Otherwise the per-step state from the analyzer
    drives the decision:

      - ``completed_with_result`` → ``"memo"`` (use cached text)
      - ``failed`` → ``"memo_failed"`` (propagate recorded failure)
      - ``pending`` / ``interrupted_with_child`` → ``"execute"``
        (re-execute; coordinator handles child cancel/adopt before
        the runtime gets here, so by the time we're executing the
        spawn step is treated as a fresh run for memo purposes)

    Kept as a free function (= no PlanRuntime coupling) so execute_plan
    free function can also be exercised with resume_plan in tests.
    """
    if resume_plan is None:
        return lambda _step_id: "execute"
    state_by_id: dict[str, str] = {}
    for s in getattr(resume_plan, "step_states", ()):
        state_by_id[s.step_id] = s.state

    def _classify(step_id: str) -> str:
        kind = state_by_id.get(step_id, "pending")
        if kind == "completed_with_result":
            return "memo"
        if kind == "failed":
            return "memo_failed"
        return "execute"

    return _classify


def _resume_memo_for(resume_plan: Any, step_id: str) -> str | None:
    if resume_plan is None:
        return None
    for s in getattr(resume_plan, "step_states", ()):
        if s.step_id == step_id and s.state == "completed_with_result":
            return s.result_text
    return None


def _resume_failure_for(resume_plan: Any, step_id: str) -> str | None:
    if resume_plan is None:
        return None
    for s in getattr(resume_plan, "step_states", ()):
        if s.step_id == step_id and s.state == "failed":
            return s.error_message
    return None


def _build_sub_loop_memo_provider(
    *,
    parent_host: Any,
    plan_id: str,
    step_id: str,
    resume_plan: Any,
) -> Any:
    """ADR-0025: construct a SubLoopMemoProvider for one step's sub-loop.

    Best-effort: if the host doesn't expose ``get_plan_registry`` (=
    test stub) or no PlanRegistry is available (= state_log not wired),
    returns None so RouterLoop runs without memoization. The plan still
    executes correctly; resume just re-pays LLM cost on crashed steps.

    Seed records on resume: extracted from
    ``resume_plan.step_llm_call_log[step_id]`` (= populated by analyzer
    from PlanSnapshot.step_llm_calls).
    """
    plan_registry = None
    getter = getattr(parent_host, "get_plan_registry", None)
    if getter is not None:
        try:
            plan_registry = getter()
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_plan_registry failed: %r", exc)
    if plan_registry is None:
        return None
    seed: list = []
    if resume_plan is not None:
        from reyn.plan import extract_step_llm_call_records
        log = getattr(resume_plan, "step_llm_call_log", None) or {}
        seed = extract_step_llm_call_records(log, step_id)
    from reyn.plan import SubLoopMemoProvider
    return SubLoopMemoProvider(
        plan_registry=plan_registry,
        plan_id=plan_id,
        step_id=step_id,
        seed_records=seed,
    )


async def execute_plan(
    plan: Plan,
    *,
    parent_host: RouterLoopHost,
    chain_id: str,
    budget: Any = None,
    router_model: str = "light",
    plan_id: str | None = None,
    resume_plan: Any = None,
    step_max_iterations: int | None = None,
    retry_limit: int | None = None,
    on_limit: Any = None,
    intervention_bus: Any = None,
    compaction_engine: "CompactionEngine | None" = None,
    step_compaction_cfg: "PlannerStepCompactionConfig | None" = None,
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

    ``plan_id``: Phase 2 step 6 — when the caller has already allocated
    the id (= ``dispatch_plan_tool`` writes the decomposition artifact
    before calling here, and the artifact is keyed on ``plan_id``), the
    caller passes it in. ``None`` keeps Phase 1 backward compat: the
    function auto-allocates uuid4-hex[:8].

    ``compaction_engine`` / ``step_compaction_cfg``: PR-N4 wiring. When
    ``compaction_engine`` is None but ``step_compaction_cfg`` is provided,
    a minimal engine is constructed lazily from ``router_model`` and the
    parent host's event log (path b: engine-per-plan-run, no session
    sharing). When neither is provided, step-results compaction is skipped
    (= legacy / no-config path, backward-compatible).
    """
    # PR-N4: lazy engine construction (path b).
    #
    # Design choice: path (b) rather than path (a) from the spec.
    # Threading the ChatSession's engine through dispatcher → RouterLoop →
    # RouterCallerState → PlanRuntime → execute_plan would require touching
    # router_loop.py and session.py (outside the ALLOWED file list for this
    # PR). Path (b) constructs a minimal engine per-plan-run from router_model.
    # Cost: one extra CompactionEngine.__init__ per plan run (= a few
    # token_counter calls at init time, cached afterwards). The engine is
    # never shared across parallel plan runs, which is safe.
    #
    # Config loading: when step_compaction_cfg is None, attempt to load it
    # from ReynConfig.plan.step_compaction so the operator's reyn.yaml
    # settings are respected without requiring caller-side changes.
    _effective_step_compaction_cfg = step_compaction_cfg
    if _effective_step_compaction_cfg is None:
        try:
            from reyn.config import load_config
            _cfg = load_config()
            _effective_step_compaction_cfg = _cfg.plan.step_compaction
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.debug(
                "execute_plan: could not load ReynConfig for step_compaction: %r; "
                "step_results compaction will be skipped",
                exc,
            )
            _effective_step_compaction_cfg = None

    _effective_engine = compaction_engine
    if _effective_engine is None and _effective_step_compaction_cfg is not None:
        try:
            from reyn.services.compaction.engine import CompactionEngine
            # T_SP=0: step-results context is not the main session SP;
            # main_pool = T_max - 0 = T_max, so threshold =
            # step_results_ratio * T_max (conservative large-window default).
            _effective_engine = CompactionEngine(
                model=router_model,
                events=parent_host.events,
                T_SP=0,
                cfg=None,  # use default CompactionConfig for budget derivation
                # #1172: router_model defaults to the class "light" — resolve
                # via the host's resolver so the engine never hands an
                # unresolved class to litellm.
                resolver=parent_host.resolver,
                # #1190 stage (ii): record planner step-results compaction spend.
                recorder=budget,
                # #1190 stage (iii) Part 4: attribute planner compaction to the host's agent.
                recorder_agent=getattr(parent_host, "agent_name", None),
            )
        except Exception as exc:  # noqa: BLE001 — best-effort; skip if unavailable
            logger.warning(
                "execute_plan: failed to construct lazy compaction engine: %r; "
                "step_results compaction will be skipped",
                exc,
            )
            _effective_engine = None

    # #1285 (#1092 plan-axis force-close, PR1): build the cumulative-current-turn
    # TurnBudgetEngine once for all steps (same router_model + resolver).
    # try_build (not build_default) so a small-context model that cannot satisfy
    # the by-construction floor DEGRADES to None (force-close inert) rather than
    # crashing — uniform with phase (PR-F3) + chat (F1). Threaded into each
    # step's _PlanStepHost below; orthogonal to step-result compaction above.
    from reyn.services.turn_budget import try_build_default_turn_budget_engine
    _plan_turn_budget_engine = try_build_default_turn_budget_engine(
        router_model,
        # getattr: test/narrow hosts may lack ``resolver`` — mirror the lazy
        # compaction-engine build above which tolerates the same. try_build then
        # degrades to None (force-close inert) rather than crashing plan exec.
        resolver=getattr(parent_host, "resolver", None),
        use_chars4=True,
    )

    # ADR-0022: allocate plan_id + record plan_started in WAL. plan_id is
    # uuid4-hex[:8] following the existing run_id allocation precedent.
    # The exception-aware finally clause mirrors ADR-0013's runtime
    # crash lifecycle: normal return / WorkflowAbortedError → plan_completed,
    # everything else (CancelledError, KeyboardInterrupt, generic Exception,
    # kill -9 path skips finally) → preserve active_plan_ids for AgentRegistry
    # restart cleanup.
    if plan_id is None:
        plan_id = uuid.uuid4().hex[:8]
    try:
        await parent_host.record_plan_started(
            plan_id=plan_id, goal=plan.goal, n_steps=len(plan.steps),
        )
    except AttributeError:
        # Test stubs / older RouterLoopHost implementations may not provide
        # the plan-lifecycle methods. Tolerate so plan-mode still functions
        # in unit-test environments without a SnapshotJournal.
        logger.debug("RouterLoopHost has no record_plan_started; skipping WAL")
    except Exception as exc:  # noqa: BLE001 — defensive, log and continue
        logger.warning("record_plan_started failed: %r", exc)

    parent_host.events.emit(
        "plan_emitted",
        chain_id=chain_id,
        plan_id=plan_id,
        goal=plan.goal,
        n_steps=len(plan.steps),
        step_ids=[s.id for s in plan.steps],
    )

    ordered = _topological_order(plan.steps)
    step_results: dict[str, str] = {}
    step_failures: dict[str, str] = {}
    total_usage = TokenUsage()

    # ADR-0023 §3.4: classify each step against the resume_plan (if any)
    # before emitting step events. Memoized steps populate step_results
    # from the recorded text WITHOUT re-executing the sub-loop or
    # re-emitting WAL step events (= those already landed pre-crash).
    resume_classifier = _build_resume_classifier(resume_plan)
    n_total = len(ordered)
    n_done = 0

    # ADR-0023 §2.1.1: surface plan-start narration so the user sees
    # progress while the plan runs in the background. Defensive — old
    # hosts may not implement put_outbox at this layer.
    try:
        await parent_host.put_outbox(
            kind="status",
            text=f"plan started ({n_total} steps)",
            meta={"plan_id": plan_id, "chain_id": chain_id, "source": "plan"},
        )
    except AttributeError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("plan start outbox emit failed: %r", exc)

    try:
        for step in ordered:
            classification = resume_classifier(step.id)
            if classification == "memo":
                # Step already completed pre-crash. Hydrate result from
                # the resume plan; skip WAL emit + sub-loop entirely.
                memo_text = _resume_memo_for(resume_plan, step.id)
                step_results[step.id] = memo_text or ""
                parent_host.events.emit(
                    "plan_step_memoized",
                    chain_id=chain_id,
                    plan_id=plan_id,
                    step_id=step.id,
                    content_len=len(memo_text or ""),
                )
                continue
            if classification == "memo_failed":
                # Step recorded as failed pre-crash. Surface failure
                # forward without re-executing.
                memo_err = _resume_failure_for(resume_plan, step.id)
                step_failures[step.id] = memo_err or "step_failed"
                parent_host.events.emit(
                    "plan_step_memo_failed",
                    chain_id=chain_id, plan_id=plan_id, step_id=step.id,
                    error=memo_err or "",
                )
                continue
            # ADR-0023 Phase 2 step 6: route plan_step_started through
            # host's WAL recorder (= persists on the durable log so the
            # resume analyzer can pair it with completed/failed). The
            # forensic events.emit stays for the legacy events log.
            try:
                await parent_host.record_plan_step_started(
                    plan_id=plan_id, step_id=step.id,
                    depends_on=list(step.depends_on),
                    n_tools=len(step.tools),
                )
            except AttributeError:
                pass  # test stub without record_plan_step_*
            except Exception as exc:  # noqa: BLE001
                logger.warning("record_plan_step_started failed: %r", exc)
            parent_host.events.emit(
                "plan_step_started",
                chain_id=chain_id,
                plan_id=plan_id,
                step_id=step.id,
                depends_on=list(step.depends_on),
                n_tools=len(step.tools),
            )
            # PR-N4: pre-frame step_results compaction. If accumulated prior
            # step outputs would balloon this step's sys_prompt, compact
            # older entries into a summary before building the prompt.
            # Best-effort: compact_step_results never raises on LLM error.
            if _effective_engine is not None and _effective_step_compaction_cfg is not None:
                from reyn.services.compaction.engine import (
                    compact_step_results,
                )
                step_results = await compact_step_results(
                    step_results,
                    engine=_effective_engine,
                    cfg=_effective_step_compaction_cfg,
                    events=parent_host.events,
                )
            narrow_host = _PlanStepHost(
                plan=plan, step=step, prior_results=step_results, parent=parent_host,
                turn_budget_engine=_plan_turn_budget_engine,
            )
            sys_prompt = build_plan_step_system_prompt(
                plan, step, step_results,
                output_language=narrow_host.output_language,
            )

            # ADR-0025: construct a per-step SubLoopMemoProvider so the
            # sub-loop's LLM calls are memoized (= recorded on every fresh
            # call, replayed on resume). seed_records carries any
            # previously-recorded LLM call results from
            # resume_plan.step_llm_call_log[step.id] (= None for fresh
            # runs, populated for resume hits).
            memo_provider = _build_sub_loop_memo_provider(
                parent_host=parent_host,
                plan_id=plan_id,
                step_id=step.id,
                resume_plan=resume_plan,
            )

            sub_loop = RouterLoop(
                host=narrow_host,
                chain_id=chain_id,
                max_iterations=step_max_iterations or _PLAN_STEP_MAX_ITERATIONS,
                router_model=router_model,
                budget=budget,
                system_prompt_override=sys_prompt,
                # Drop `plan` from the step's tool catalog so the step LLM cannot
                # recursively decompose into another plan. Without this, the step
                # LLM sees `plan` as available and may self-decompose, causing
                # unbounded recursion (= dogfood 2026-05-07: 3-step plan emitted
                # 3 plan invocations because steps re-emitted plan).
                exclude_tools={"plan"},
                memo_provider=memo_provider,
                # #187: empty-stop retry — shared uniform "resume" directive +
                # always-on (owner decision; env-gate retired, no per-site
                # differentiation).
                empty_stop_retry_directive=EMPTY_STOP_RETRY_DIRECTIVE,
                empty_stop_retry_auto=True,
                # FP-0005: wire safety.on_limit so step_max_iterations exhaustion
                # routes through handle_limit_exceeded instead of flat-aborting.
                on_limit=on_limit,
            )
            # FP-0031-C: auto-retry on transient step failures.
            # FP-0031-D: when retry budget is exhausted, ask the user via
            # handle_limit_exceeded; on approval extend by the original limit.
            _base_retry_limit: int = retry_limit if retry_limit is not None else _PLAN_STEP_RETRY_LIMIT
            # step_retry_limit is mutable — Component D extends it on approval.
            step_retry_limit = _base_retry_limit
            last_exc: Exception | None = None
            step_succeeded = False
            desc_preview = (step.description or step.id)[:60]
            attempt = 0
            # #1285 PR2 (#1092 plan-axis re-entry, Q-A): force-close re-entry
            # state, DISTINCT from FP-0031 ``attempt`` (cumulative-context vs
            # transient-failure trigger — never conflated). On force-close the
            # step re-enters the SAME step from the bounded consolidation
            # (continuation, not restart), bounded by the by-construction floor
            # (PR1 assert_turn_budget_bounds) + the defensive cap above.
            fc_reentries = 0
            _seed_user_text = step.description
            # Issue #214 (= #180 #2 split): set the plan_step contextvar
            # for the duration of this step's sub-loop. The ContextVar
            # propagates through any asyncio.Task the sub-loop spawns
            # (= invoke_skill → skill_runner → Agent.run), so the spawned
            # skill's OSRuntime / EventLog stamps "plan N/M" into every
            # event and the TUI's SkillActivityRow can render the plan
            # context as detail. ``n_done`` is the 1-based step number
            # currently executing (= already-completed count + 1).
            from reyn.skill._plan_step_context import set_plan_step
            while attempt <= step_retry_limit:
                try:
                    with set_plan_step(
                        n_done=n_done + 1,
                        n_total=n_total,
                        step_id=step.id,
                    ):
                        sub_usage = await sub_loop.run(
                            user_text=_seed_user_text, history=[],
                        )
                    if sub_usage is not None:
                        total_usage.prompt_tokens += sub_usage.prompt_tokens
                        total_usage.completion_tokens += sub_usage.completion_tokens
                    # #1285 PR2: force-close re-entry (the no-exception success
                    # path — mutually exclusive with the transient-retry except
                    # branch below). If the step force-closed and the re-entry cap
                    # is not yet hit, re-enter the SAME step from the bounded
                    # consolidation (continuation via the next user turn, NOT a
                    # restart). ``attempt`` RESETS to 0 (fresh transient-retry
                    # budget for the re-entered turn-set); ``fc_reentries`` is the
                    # SEPARATE force-close counter (never conflated with attempt).
                    # On cap → fall through to the FLOOR (the last consolidation is
                    # the step output via the capture below).
                    _fc = narrow_host.forced_close_result
                    if _fc is not None and fc_reentries < _PLAN_STEP_MAX_FORCE_CLOSE_REENTRIES:
                        fc_reentries += 1
                        _seed_user_text = _PLAN_STEP_FORCE_CLOSE_CONTINUE_TEMPLATE.format(
                            consolidation=(getattr(_fc, "content", None) or ""),
                        )
                        attempt = 0  # reset transient-retry budget for the re-entry
                        narrow_host = _PlanStepHost(
                            plan=plan, step=step,
                            prior_results=step_results, parent=parent_host,
                            turn_budget_engine=_plan_turn_budget_engine,
                        )
                        sub_loop = RouterLoop(
                            host=narrow_host,
                            chain_id=chain_id,
                            max_iterations=step_max_iterations or _PLAN_STEP_MAX_ITERATIONS,
                            router_model=router_model,
                            budget=budget,
                            system_prompt_override=sys_prompt,
                            exclude_tools={"plan"},
                            memo_provider=memo_provider,
                            # #187: empty-stop retry — shared uniform "resume"
                            # directive + always-on (owner decision).
                            empty_stop_retry_directive=EMPTY_STOP_RETRY_DIRECTIVE,
                            empty_stop_retry_auto=True,
                        )
                        continue  # re-enter — does NOT consume the FP-0031 attempt budget
                    step_succeeded = True
                    break  # success (or force-close cap reached → FLOOR) — exit retry loop
                except _PLAN_RETRY_EXCLUDED as exc:
                    raise  # delegate to safety layer / abort path
                except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                    raise  # system signals — never retry
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    attempt += 1
                    # WorkflowAbortedError — deliberate step termination per
                    # ADR-0013. Record as step failure and let the plan continue
                    # without retry (= the step is done intentionally).
                    if _is_workflow_abort(type(exc)):
                        break
                    if attempt <= step_retry_limit:
                        # Retry available — emit status and rebuild sub-loop.
                        try:
                            await parent_host.put_outbox(
                                kind="status",
                                text=f"  リトライ {attempt}/{step_retry_limit}: {desc_preview}",
                                meta={
                                    "plan_id": plan_id, "chain_id": chain_id,
                                    "step_id": step.id, "source": "plan",
                                },
                            )
                        except AttributeError:
                            pass
                        except Exception as exc2:  # noqa: BLE001
                            logger.warning("plan step retry outbox emit failed: %r", exc2)
                        # Rebuild narrow_host and sub_loop for the retry so
                        # captured_text is fresh (= the old host may have
                        # partial state from the failed attempt).
                        narrow_host = _PlanStepHost(
                            plan=plan, step=step,
                            prior_results=step_results, parent=parent_host,
                            turn_budget_engine=_plan_turn_budget_engine,
                        )
                        sub_loop = RouterLoop(
                            host=narrow_host,
                            chain_id=chain_id,
                            max_iterations=step_max_iterations or _PLAN_STEP_MAX_ITERATIONS,
                            router_model=router_model,
                            budget=budget,
                            system_prompt_override=sys_prompt,
                            exclude_tools={"plan"},
                            memo_provider=memo_provider,
                            # B42-NF-W6-1: same directive as the initial
                            # sub_loop construction above.
                            # #187: empty-stop retry — shared uniform "resume"
                            # directive + always-on (owner decision).
                            empty_stop_retry_directive=EMPTY_STOP_RETRY_DIRECTIVE,
                            empty_stop_retry_auto=True,
                        )
                        continue
                    # FP-0031-D: retry budget exhausted — ask user for extension.
                    if on_limit is not None:
                        try:
                            from reyn.safety.limit_handler import handle_limit_exceeded
                            err_preview = str(last_exc)[:120]
                            limit_decision = await handle_limit_exceeded(
                                bus=intervention_bus,
                                on_limit=on_limit,
                                kind=f"plan_step_retry:{step.id}",
                                run_id=plan_id or chain_id,
                                prompt=(
                                    f"Plan step '{desc_preview}' has failed "
                                    f"{step_retry_limit} times. "
                                    f"Allow {_base_retry_limit} more retries?"
                                ),
                                detail=f"Last error: {err_preview}",
                                extension_amount=float(_base_retry_limit),
                            )
                            if limit_decision.allow_continue:
                                # User approved extension — add base_limit more attempts.
                                step_retry_limit += _base_retry_limit
                                continue  # back to retry loop
                        except Exception as exc_d:  # noqa: BLE001
                            logger.warning(
                                "plan step retry limit_handler failed: %r", exc_d,
                            )
                    # No extension granted (or on_limit=None) — fall through to failure.
                    break

            if not step_succeeded:
                exc = last_exc  # type: ignore[assignment]
                step_failures[step.id] = repr(exc)
                try:
                    await parent_host.record_plan_step_failed(
                        plan_id=plan_id, step_id=step.id, error=repr(exc),
                    )
                except AttributeError:
                    pass
                except Exception as exc2:  # noqa: BLE001
                    logger.warning("record_plan_step_failed failed: %r", exc2)
                parent_host.events.emit(
                    "plan_step_failed",
                    chain_id=chain_id,
                    plan_id=plan_id,
                    step_id=step.id,
                    error=repr(exc),
                )
                # FP-0031-B: emit failure status so the user sees which step
                # failed and a short error summary while the plan continues.
                err_summary = str(exc)[:80]
                try:
                    await parent_host.put_outbox(
                        kind="status",
                        text=f"plan step {n_done + 1}/{n_total}: {desc_preview} → 失敗 ({err_summary})",
                        meta={
                            "plan_id": plan_id, "chain_id": chain_id,
                            "step_id": step.id, "source": "plan",
                        },
                    )
                except AttributeError:
                    pass
                except Exception as exc3:  # noqa: BLE001
                    logger.warning("plan step failure outbox emit failed: %r", exc3)
                continue

            text = narrow_host.captured_text
            # #1285 (#1092 plan-axis force-close, PR1 FLOOR): if this step
            # force-closed (the cumulative-current-turn trigger fired), the
            # bounded wrap-up consolidation went to record_force_close, NOT the
            # normal put_outbox capture — so captured_text may be empty/stale.
            # Use the consolidation as the step's contribution: the step ends
            # bounded with a clean summary instead of overflowing. (PR2 re-enters
            # the SAME step from this consolidation; PR1 is the proven floor.)
            _fc_result = narrow_host.forced_close_result
            if _fc_result is not None:
                text = getattr(_fc_result, "content", None) or text
            step_results[step.id] = text
            try:
                await parent_host.record_plan_step_completed(
                    plan_id=plan_id, step_id=step.id, content_len=len(text),
                    result_text=text,
                )
            except AttributeError:
                pass
            except TypeError:
                # ADR-0023 Phase 2 v1 signature compat: hosts that
                # don't take result_text get the legacy 3-arg call.
                try:
                    await parent_host.record_plan_step_completed(
                        plan_id=plan_id, step_id=step.id,
                        content_len=len(text),
                    )
                except AttributeError:
                    pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("record_plan_step_completed failed: %r", exc)
            # ADR-0023 §2.1.1: emit step progress narration so the user
            # sees the plan progressing while it runs in the background.
            n_done += 1
            desc_preview = (step.description or step.id)[:60]
            try:
                await parent_host.put_outbox(
                    kind="status",
                    text=f"plan step {n_done}/{n_total}: {desc_preview}",
                    meta={
                        "plan_id": plan_id, "chain_id": chain_id,
                        "step_id": step.id, "source": "plan",
                    },
                )
            except AttributeError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("plan step outbox emit failed: %r", exc)
            parent_host.events.emit(
                "plan_step_completed",
                chain_id=chain_id,
                plan_id=plan_id,
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
            plan_id=plan_id,
            n_completed=len(step_results),
            n_failed=len(step_failures),
            result_len=len(final_text),
        )
    finally:
        # ADR-0013 pattern: classify the exit by exc_info to decide whether
        # to mark the plan completed (= normal/WorkflowAbortedError) or
        # leave it in active_plan_ids for restart-time cleanup (= crash
        # / cancel / generic Exception).
        exc_type = sys.exc_info()[0]
        if exc_type is None or _is_workflow_abort(exc_type):
            try:
                await parent_host.record_plan_completed(plan_id=plan_id)
            except AttributeError:
                pass  # test stub
            except Exception as exc:  # noqa: BLE001
                logger.warning("record_plan_completed failed: %r", exc)
        else:
            # Crash / cancel: emit interrupted audit event but DO NOT prune
            # active_plan_ids — AgentRegistry.restore_all post-replay will
            # discover the orphan, cancel its child skills, and notify user.
            try:
                parent_host.events.emit(
                    "plan_run_interrupted",
                    chain_id=chain_id,
                    plan_id=plan_id,
                    exc_type=exc_type.__name__,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("plan_run_interrupted emit failed: %r", exc)

    return PlanExecutionResult(
        text=final_text,
        step_results=step_results,
        step_failures=step_failures,
        usage=total_usage,
        plan_goal=plan.goal,
        n_steps=len(plan.steps),
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

    **ADR-0023 Phase 2.1: async dispatch.** ``plan`` is registered as
    ``dispatch_kind="async"`` on its ToolDefinition (resolved via
    ``get_dispatch_kind("plan")`` → registry lookup); this function
    spawns the runtime as a background task and returns the spawn ack
    immediately. The
    RouterLoop sees an async tool result and exits the chat turn —
    the user gets quick replies first while the plan runs in the
    background, mirroring ``_spawn_skill`` UX.

    Lifecycle ordering (= ADR-0023 §3.5 invariant preserved):

      1. Validate plan.
      2. Allocate ``plan_id`` + per-plan ``chain_id`` (= ADR-0023
         §2.1.2 — each plan registers its own chain so R-D14
         cross-agent notify works for ``/plan discard``).
      3. Write decomposition artifact (= P5 SSoT for resume).
      4. Construct ``PlanRuntime(plan_id=…, chain_id=plan_chain_id)``.
      5. Hand off to ``host.spawn_plan_task`` — ChatSession owns the
         task lifecycle, terminal-text outbox emit, and decomposition
         cleanup on clean exit.
      6. Return ``{"status": "spawned", "plan_id": ..., ...}``.

    On crash mid-flight: ``running_plans`` task dies, decomposition
    artifact stays for restart-time resume (= ADR-0023 §3.4 / §3.5).

    The ``chain_id`` parameter is the **chat-turn** chain (= caller
    context). The plan-internal chain_id is allocated here as
    ``plan_<plan_id>`` so R-D14 notifications target the right
    waiter on plan discard.
    """
    try:
        plan = parse_and_validate_plan(args, allowed_tool_names=available_tool_names)
    except PlanValidationError as exc:
        return {
            "status": "error",
            "error": {"kind": "plan_invalid", "message": str(exc)},
        }

    # Step 6: allocate plan_id + write decomposition artifact BEFORE
    # ``plan_started`` lands in WAL. Any plan in ``active_plan_ids`` MUST
    # have a discoverable decomposition (= ADR-0023 §3.5).
    plan_id = uuid.uuid4().hex[:8]
    plan_chain_id = f"plan_{plan_id}"  # ADR-0023 §2.1.2 — per-plan chain

    try:
        await parent_host.write_plan_decomposition(plan_id=plan_id, plan=plan)
    except AttributeError:
        # Test stub without artifact persistence — Phase 2 v1 tolerates,
        # resume won't be possible but fresh-run path still works.
        logger.debug(
            "RouterLoopHost has no write_plan_decomposition; skipping artifact",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("write_plan_decomposition failed: %r", exc)

    # Construct the runtime; ChatSession spawns it as a task and owns
    # the lifecycle (= terminal outbox emit + artifact cleanup).
    from reyn.plan import PlanRuntime
    runtime = PlanRuntime(
        plan,
        host=parent_host,
        chain_id=plan_chain_id,           # per-plan chain
        plan_id=plan_id,
        budget=budget,
        router_model=router_model,
    )

    # Capability detection via hasattr (= NOT try/except) so the
    # synchronous fallback runs in clean exception context. If the
    # fallback ran inside an except AttributeError block, execute_plan's
    # finally clause's sys.exc_info() check would mis-classify the
    # outer-caught AttributeError as a "crash" and skip
    # record_plan_completed. ADR-0023 §2.1.1 async path is the
    # production path; the sync fallback is a test-stub safety net.
    if hasattr(parent_host, "spawn_plan_task"):
        # Batch 16 / G27: pass parent_chain_id so spawn_plan_task's
        # history append for the terminal text is tagged with the
        # caller's (= A2A / chat turn) chain, not the per-plan chain.
        # This makes _new_agent_history_entries filter pick up the
        # plan reply for the original A2A request. Backward-compatible
        # via try/except for hosts that don't accept the new kwarg.
        try:
            await parent_host.spawn_plan_task(
                plan_id=plan_id, runtime=runtime,
                chain_id=plan_chain_id,
                parent_chain_id=chain_id,
            )
        except TypeError:
            # Older host signature without parent_chain_id — fall back.
            await parent_host.spawn_plan_task(
                plan_id=plan_id, runtime=runtime, chain_id=plan_chain_id,
            )
        return {
            "status": "spawned",
            "plan_id": plan_id,
            "chain_id": plan_chain_id,
            "n_steps": len(plan.steps),
        }

    # Synchronous fallback for hosts without spawn_plan_task (= test
    # stubs / lightweight integrations). Same lifecycle as Phase 2 v1.
    logger.debug(
        "RouterLoopHost has no spawn_plan_task; running plan synchronously",
    )
    clean_exit = False
    try:
        result = await runtime.run()
        clean_exit = True
        return {
            "status": "ok",
            "text": result.text,
            "step_results": result.step_results,
            "step_failures": result.step_failures,
            "n_steps": len(plan.steps),
        }
    except BaseException as exc:
        if _is_workflow_abort(type(exc)):
            clean_exit = True
        raise
    finally:
        if clean_exit:
            try:
                await parent_host.delete_plan_decomposition(plan_id=plan_id)
            except AttributeError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("delete_plan_decomposition failed: %r", exc)


__all__ = [
    "Plan",
    "PlanStep",
    "PlanExecutionResult",
    "PlanValidationError",
    "_PLAN_RETRY_EXCLUDED",
    "_PLAN_STEP_RETRY_LIMIT",
    "build_plan_step_system_prompt",
    "dispatch_plan_tool",
    "execute_plan",
    "parse_and_validate_plan",
]
