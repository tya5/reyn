"""The narrowed-LLM execution engine — a ``RouterLoopHost`` facade.

Narrows the parent host's tool catalog + system-prompt surface to ONE unit of
work, so a per-unit LLM call stays small: each ``list_*`` / ``get_*`` introspection
method returns the parent's data only when the unit's ``tools`` ask for that
family (skills / agents / file / mcp / web — matched by legacy or qualified
``<category>__*`` name), else empty / None. Tool dispatch passes through to the
parent. The engine captures the unit's reply text (``put_outbox`` on the ``agent``
kind) for the caller to collect, and exposes the force-close / retry interface the
router loop consults each turn.
"""
from __future__ import annotations

from typing import Any

from reyn.runtime.router_loop import RouterLoopHost

# Tool families that need their respective host data plumbed through.
# Used by ``TaskExecutionHost`` to decide whether a given host method should
# return narrow data or be silenced (= return empty / None).
_FILE_TOOL_NAMES = frozenset({"list_directory", "read_file", "write_file", "delete_file"})
_MCP_TOOL_NAMES = frozenset({"list_mcp_servers", "list_mcp_tools", "call_mcp_tool", "describe_mcp_tool"})
_WEB_FETCH_TOOL_NAME = "web_fetch"
_INVOKE_SKILL_TOOL_NAME = "invoke_skill"
_DELEGATE_TOOL_NAME = "delegate_to_agent"


class TaskExecutionHost:
    """RouterLoopHost facade narrowing scope to one plan step.

    Every method either passes through to the parent host (for tool
    dispatch) or narrows what RouterLoop sees when building the catalog
    (for ``list_*`` / ``get_*`` introspection methods). The narrowing
    is what makes per-step LLM calls small: if a step's tools doesn't
    include ``list_skills``, the parent's 25-skill list never reaches
    the step's system prompt or tool schema.
    """

    @classmethod
    def for_task(
        cls,
        task: Any,
        *,
        parent: RouterLoopHost,
        prior_results: "dict[str, str] | None" = None,
        turn_budget_engine: Any = None,
    ) -> "TaskExecutionHost":
        """Task-driven construction (#1953 slice P2): narrow to the **Task**'s
        ``tools``. The engine is unit-agnostic — it reads only ``.tools`` (a Task
        and a plan-step both carry it) + captures the reply text — so this is an
        additive entry that leaves the plan-step path byte-identical.
        ``prior_results`` is the dep-results map the caller assembled from the
        backend (the result-channel; which dep feeds the unit is the caller's /
        LLM's concern, not a graph property — I-2)."""
        return cls(plan=None, step=task, prior_results=prior_results or {},
                   parent=parent, turn_budget_engine=turn_budget_engine)

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
        # The narrowed tool set — read off the UNIT (a plan-step or a Task; both
        # carry ``.tools``). The engine is otherwise unit-agnostic.
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

    def _uses_family(self, legacy: frozenset[str], qualified_prefix: str) -> bool:
        """True when this step's tools name a member of a tool family — by its
        LEGACY name OR its qualified ``<category>__*`` name (#1984).

        Default plans use the **qualified** names (#1657 enumerate-all flat-lists
        ``skill__x`` / ``file__read`` / …), while pre-#1657 / self-contained plans
        use the **legacy** names (``invoke_skill`` / ``read_file`` / …). The narrow
        host's per-family plumbing must recognize BOTH, else a default-mode step
        that names ``skill__x`` is silently starved of the skill catalog (its
        ``available_skills`` resolves to ``[]`` → the universal catalog drops the
        skill category). Purely additive: a legacy-name step is byte-identical."""
        return bool(self._tool_set & legacy) or any(
            t.startswith(qualified_prefix) for t in self._tool_set
        )

    def list_available_skills(self) -> list[dict]:
        # Skills only visible if the step asked for them (invoke_skill / describe_skill
        # legacy, or any qualified skill__* — #1984: the latter was the live break).
        if self._uses_family(frozenset({_INVOKE_SKILL_TOOL_NAME, "describe_skill"}), "skill__"):
            return self._parent.list_available_skills()
        return []

    def list_available_agents(self) -> list[dict]:
        if self._uses_family(frozenset({_DELEGATE_TOOL_NAME, "describe_agent"}), "multi_agent__"):
            return self._parent.list_available_agents()
        return []

    def get_memory_index(self) -> dict:
        if self._uses_family(frozenset({"list_memory", "read_memory_body"}), "memory_operation__"):
            return self._parent.get_memory_index()
        return {"status": "not_found", "content": ""}

    def get_file_permissions(self) -> dict | None:
        # Consumed by the ``universal`` scheme's build_tools (the enumerate-all
        # default catalogs file as a static category, so this is masked there —
        # but the universal scheme is config-selectable, so keep it correct, #1984).
        if self._uses_family(_FILE_TOOL_NAMES, "file__"):
            return self._parent.get_file_permissions()
        return None

    def get_mcp_servers(self) -> list[dict]:
        if self._uses_family(_MCP_TOOL_NAMES, "mcp__"):
            return self._parent.get_mcp_servers()
        return []

    def get_web_fetch_allowed(self) -> bool:
        # FP-0022: web_fetch is always allowed at the catalog level; authorization
        # is enforced at the handler level. Return True when the step's tool_set
        # includes web_fetch (legacy or qualified web__*, #1984).
        return self._uses_family(frozenset({_WEB_FETCH_TOOL_NAME}), "web__")

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
