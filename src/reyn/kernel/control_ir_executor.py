"""
Control IR Executor — dynamic frontend for op_runtime.

Receives a list of ControlIROp instances emitted by the LLM, applies
phase-level allow-list filtering, then delegates each op to op_runtime.
Permission checks, event emission, and the per-op error envelope all
live in op_runtime so the same backend serves the static (preprocessor)
frontend as well.

Workspace owns data; op_runtime owns execution; this module owns
LLM-act-turn dispatch policy.

Each op invocation is wrapped with dispatch_tool (PR37 wave 2C) which
adds tool_called / tool_returned / tool_failed event brackets and a
uniform error shape. The existing tool_executed events from op_runtime
handlers are preserved (they run inside the invoker).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reyn.config import MultimodalConfig, SandboxConfig
    from reyn.security.sandbox.backend import SandboxBackend
    from reyn.security.secrets.store import ScopedSecretStore
    from reyn.workspace.media_store import MediaStore

from reyn.dispatch import DispatchContext, dispatch_tool
from reyn.events.events import EventLog
from reyn.llm.model_resolver import ModelResolver
from reyn.op_runtime import execute_op
from reyn.op_runtime.context import OpContext
from reyn.schemas.models import (
    ControlIROp,
    ControlIROpSpec,
)
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import RequestBus
from reyn.workspace.workspace import Workspace


def _build_phase_tool_catalog(allowed_ops: set[str]) -> dict[str, dict]:
    """Build a tool_catalog for dispatch_tool from a set of allowed op kinds.

    Each allowed op kind becomes a tool entry whose parameters schema comes
    from the unified ToolRegistry (= ADR-0026 Phase 4-3).  Each
    ToolDefinition with ``gates.phase == "allow"`` carries the IROp-derived
    schema; the registry is now the single source for both schema rendering
    and dispatch.

    Unknown / router-only kinds get a schema-less entry (= no arg
    validation; dispatch will return ``unknown_tool`` if invoked).

    #1240 Wave 2b: D7 verb-drop removed (coarse "file" kind dropped; all
    file ops are now plain fine kinds: read_file/write_file/etc.). The
    split_tool_name call and _drop_schema_property helper are no longer
    needed here; each name is looked up directly in the registry.

    Returns a dict[str, dict] in litellm tools= entry shape:
        {op_kind: {"function": {"name": op_kind, "parameters": <json schema>}}}
    """
    from reyn.tools import get_default_registry
    registry = get_default_registry()

    catalog: dict[str, dict] = {}
    for name in allowed_ops:
        tool_def = registry.lookup(name)
        if tool_def is None or tool_def.gates.phase != "allow":
            catalog[name] = {"function": {"name": name}}
            continue
        catalog[name] = {
            "function": {
                "name": name,
                "parameters": dict(tool_def.parameters),
            }
        }
    return catalog


class ControlIRExecutor:
    def __init__(
        self,
        workspace: Workspace,
        events: EventLog,
        intervention_bus: RequestBus | None = None,
        resolver: ModelResolver | None = None,
        permission_resolver: PermissionResolver | None = None,
        max_phase_visits: int = 25,
        skill_name: str = "",
        mcp_servers: dict | None = None,
        caller: str = "direct",
        chain_id: str | None = None,
        state_log: Any = None,
        skill_run_id: str | None = None,
        resume_plan: Any = None,
        run_id: str | None = None,
        sandbox_config: "SandboxConfig | None" = None,
        sandbox_backend: "SandboxBackend | None" = None,
        multimodal_config: "MultimodalConfig | None" = None,
        media_store: "MediaStore | None" = None,
        secret_store: "ScopedSecretStore | None" = None,
        budget_tracker: object | None = None,
    ) -> None:
        self.workspace = workspace
        self.events = events
        # #1190 stage (ii): cost recorder for LLM-calling ops (judge_output).
        self._budget_tracker = budget_tracker
        self._intervention_bus = intervention_bus
        self._max_phase_visits = max_phase_visits
        self._resolver = resolver or ModelResolver({})
        self._perm = permission_resolver
        self._skill_name = skill_name
        self._mcp_servers: dict = (mcp_servers or {}).get("servers", {})
        self._mcp_clients: dict = {}  # cached across ops
        self._caller = caller
        self._chain_id = chain_id
        # FP-0021: run_id of the currently-executing OSRuntime run.
        # Propagated into OpContext so event helpers can stamp run scope.
        self._run_id = run_id
        # PR-skill-resume part A: WAL plumbing for step-event emission.
        # When ``state_log`` and ``skill_run_id`` are wired, ``execute()``
        # threads them into DispatchContext so dispatch_tool emits
        # step_started / step_completed / step_failed alongside audit
        # events. CLI / standalone runs leave these unset → no step
        # events (no resume context anyway).
        self._state_log = state_log
        self._skill_run_id = skill_run_id
        # PR-skill-resume D3b-2: optional ResumePlan from
        # SkillResumeAnalyzer. Threaded into DispatchContext so
        # dispatch_tool memoizes against committed_steps (D3b-1).
        # ``None`` means normal execution (no memoization), which is
        # the default for fresh starts.
        self._resume_plan = resume_plan
        # FP-0017 follow-up: SandboxConfig (= reyn.yaml `sandbox:` section)
        # propagated into every OpContext so sandboxed_exec backend
        # selection honors the operator's declared backend / on_unsupported
        # policy. ``None`` means the factory falls through to platform
        # auto-detection (= unchanged behavior pre-wiring).
        self._sandbox_config = sandbox_config
        # FP-0008 #1115 Stage 2: per-run injected exec backend instance. When
        # set (a dual-Protocol container backend), it takes precedence over
        # name-based platform selection in the sandboxed_exec handler
        # (``ctx.sandbox_backend or get_default_backend(...)``). ``None`` =
        # platform auto-detect (unchanged host behavior).
        self._sandbox_backend = sandbox_backend
        # Issue #364 — multi-modal media-size gate config (= reyn.yaml
        # ``multimodal:`` section). Threaded into OpContext so binary paths
        # (web__fetch / file__read / MCP / user input) can consult the cap
        # + on_oversize policy before loading large payloads into the LLM.
        # ``None`` = no cap (= permissive default for callers that don't
        # supply a ReynConfig).
        self._multimodal_config = multimodal_config
        # Issue #383 PR-C — flat-file media + tool-result storage threaded
        # to OpContext so handlers can save binary via
        # ``ctx.media_store.save_image`` and emit path-ref blocks.
        self._media_store = media_store
        # FP-0016 D: per-skill credential scoping. None = unrestricted
        # (= preserves backward compat for callers that don't supply a store).
        self._secret_store = secret_store

    @property
    def resume_plan(self) -> "Any":
        """Return the ResumePlan this executor was constructed with, or None."""
        return self._resume_plan

    @property
    def mcp_clients(self) -> dict:
        """Read-only accessor for the cached MCP client map (server name →
        client). Tests inspect this to verify the teardown lifecycle
        (= ``teardown_mcp_clients`` empties the dict). Production
        callers continue to use ``self._mcp_clients`` for the write
        side (= ops cache new clients there).
        """
        return self._mcp_clients

    @property
    def secret_store(self):
        """Read-only accessor for the injected ScopedSecretStore (or None)."""
        return self._secret_store

    # #1240 Wave 2a (catalog-build): minimal valid example per fine file kind,
    # used to build the json-mode ControlIROpSpec advertised in the frame. The
    # description comes from the unified registry (single source); the example
    # is the op shape (ControlIROpSpec requires one — it is not on ToolDefinition).
    _FINE_FILE_OP_EXAMPLES: dict[str, dict[str, Any]] = {
        "read_file":   {"kind": "read_file", "path": "src/module.py"},
        "write_file":  {"kind": "write_file", "path": "out.txt", "content": "full file text"},
        "edit_file":   {"kind": "edit_file", "path": "src/module.py", "old_string": "old", "new_string": "new"},
        "delete_file": {"kind": "delete_file", "path": "tmp/scratch.txt"},
        "glob_files":  {"kind": "glob_files", "path": ".", "pattern": "**/*.py"},
        "grep_files":  {"kind": "grep_files", "path": "src", "pattern": "def \\w+", "glob": "**/*.py"},
    }

    def _fine_file_op_specs(self) -> list[ControlIROpSpec]:
        """#1240 Wave 2a: fine-grained file op specs for the json-mode frame.

        Derived from the unified ToolRegistry (the SAME phase=allow
        ToolDefinitions the op-loop catalog ``_build_phase_tool_catalog`` uses —
        single source for the description), so a json-mode phase that migrated
        its ``allowed_ops`` to fine names (read_file/write_file/…) gets matching
        ``available_control_ops`` in the frame (the LLM SEES + emits the fine
        kinds). Advertised alongside the coarse ``file`` spec; ``build_frame``
        filters per phase by ``allowed_ops``, so an un-migrated skill (coarse
        ``[file]``) still sees only the coarse spec — behavior-preserving.

        Closes the catalog-build gap (#997-class) the Wave-1 β-obviation proof
        missed: that proof exercised the op-loop dispatch path
        (``_build_phase_tool_catalog`` → executor.execute) and bypassed this
        json-mode frame advertisement path (surfaced by the Wave-2a dogfood —
        migrated phase advertised coarse ``file`` / allowed fine → LLM emitted
        the coarse ``file`` it was shown → executor skipped it).
        """
        from reyn.tools import get_default_registry
        registry = get_default_registry()
        specs: list[ControlIROpSpec] = []
        for name, example in self._FINE_FILE_OP_EXAMPLES.items():
            tool_def = registry.lookup(name)
            if tool_def is None or tool_def.gates.phase != "allow":
                continue
            specs.append(ControlIROpSpec(
                kind=name, description=tool_def.description, example=example,
            ))
        return specs

    def available_ops(self) -> list[ControlIROpSpec]:
        """Return the Control IR op kinds this executor advertises to the LLM.

        These specs flow into ContextFrame.available_control_ops; the LLM picks
        from this list when emitting `control_ir`. Op kinds are filtered per
        runtime config (mcp_servers configured) and per phase by
        ``allowed_ops`` in ``build_frame``.
        """
        return [
            # #1240 Wave 2b: coarse "file" op dropped — all file ops use fine kinds.
            # build_frame filters per phase by allowed_ops.
            *self._fine_file_op_specs(),
            ControlIROpSpec(
                kind="ask_user",
                description=(
                    "Pause the phase and ask the user a clarifying question. "
                    "The user's response is injected into user_responses in the next ContextFrame "
                    "and the same phase re-runs with the original input_artifact unchanged. "
                    "Use when required data is missing and cannot be inferred."
                ),
                example={
                    "kind": "ask_user",
                    "question": "What should the app be named?",
                    "suggestions": ["qa_app", "research_app"],
                    "required": True,
                },
            ),
            # #1352-A/L3: the deprecated `shell` op (raw subprocess) was removed;
            # use `sandboxed_exec` below.
            ControlIROpSpec(
                kind="sandboxed_exec",
                description=(
                    "Execute a command under a sandbox policy and return stdout, stderr, "
                    "and returncode. "
                    "argv: the command as a LIST of strings — argv[0] is the executable, "
                    "e.g. [\"python\", \"-m\", \"pytest\", \"-x\"]. It is NOT a single shell "
                    "string and NOT a 'command'/'cmd' field — always provide argv as a list. "
                    "There is no shell: pipes, redirects (>, 2>&1), globs, and &&/|| are NOT "
                    "interpreted — pass real argv tokens only. "
                    "Runs in the project root directory; stdout and stderr are captured "
                    "separately (do not append redirections). "
                    "Optional policy fields (network: bool, read_paths/write_paths: list[str], "
                    "allow_subprocess: bool, env_passthrough: list[str], timeout_seconds: int) "
                    "may be omitted — when the phase declares a default sandbox policy the OS "
                    "applies it and overrides them. "
                    "Use for git, pytest, build, and other repository commands. "
                    "Status: enabled by default — issue sandboxed_exec ops directly; do not "
                    "abort on permission concerns."
                ),
                example={"kind": "sandboxed_exec", "argv": ["python", "-m", "pytest", "-x", "--tb=short"]},
            ),
            *([ControlIROpSpec(
                # #1240 Wave 2b (A)-alias: advertise as "call_mcp_tool" (chat name)
                # — the op-loop and json-mode parse paths rewrite this to "mcp"
                # before ControlIROp validation.  allowed_ops=[mcp] still works
                # via _PHASE_TOOL_NAME_ALIAS in build_frame.
                kind="call_mcp_tool",
                description=(
                    "Call a tool on a configured MCP server (HTTP transport). "
                    "server: the server name as defined in mcp.servers config. "
                    "tool: the tool name exposed by that server. "
                    "args: arguments dict to pass to the tool. "
                    "Returns: content (text), raw (full MCP result). "
                    "Status: enabled — this op's presence in op_catalog means "
                    "mcp permission is verified for this phase. Issue call_mcp_tool ops "
                    "directly; do not abort on permission concerns."
                ),
                example={"kind": "call_mcp_tool", "server": "my_tool", "tool": "search", "args": {"query": "hello"}},
            )] if self._mcp_servers else []),
            ControlIROpSpec(
                kind="lint",
                description=(
                    "Run the DSL linter against a skill directory and return issues. "
                    "skill_path: workspace-relative path to the skill directory (e.g. 'reyn/local/my_skill'). "
                    "Returns: passed (bool), error_count, warning_count, issues (list of strings)."
                ),
                example={"kind": "lint", "skill_path": "reyn/local/my_skill"},
            ),
            ControlIROpSpec(
                # #1240 Wave 2b (A)-alias: advertise as "invoke_skill" (chat name)
                # — the op-loop and json-mode parse paths rewrite this to "run_skill"
                # before ControlIROp validation.  allowed_ops=[run_skill] still
                # works via _PHASE_TOOL_NAME_ALIAS in build_frame.
                kind="invoke_skill",
                description=(
                    "Run a reyn skill in-process and return its final output. "
                    "skill: skill name (resolved via search path) or path to skill.md. "
                    "input: input artifact dict to pass to the sub-skill. "
                    "model: model class or LiteLLM string (default: inherit from runtime). "
                    "workspace: 'isolated' (default) creates a sub-workspace; 'shared' uses the current workspace. "
                    "Returns: status ('finished'|'loop_limit_exceeded'), final_output (dict), "
                    "phase_artifacts (list of {phase, artifact, path} for each intermediate phase output), "
                    "token_usage (prompt_tokens, completion_tokens)."
                ),
                example={"kind": "invoke_skill", "skill": "my_skill", "input": {"type": "user_message", "data": {"text": "hello"}}},
            ),
            ControlIROpSpec(
                kind="web_fetch",
                description=(
                    "Fetch a URL and return its content as plain text. "
                    "HTML pages are converted to readable text (tags stripped). "
                    "url: the URL to fetch (http or https). "
                    "prompt: optional hint describing what to extract — informational for the LLM, not used in fetching. "
                    "timeout: request timeout in seconds (default 30). "
                    "max_length: cap on returned content in characters (default 50000). "
                    "Returns: url, status_code, content_type, content (text), truncated (bool)."
                ),
                example={"kind": "web_fetch", "url": "https://example.com", "prompt": "Get the main article text"},
            ),
            ControlIROpSpec(
                kind="web_search",
                description=(
                    "Search the web and return structured results. "
                    "query: the search query string. "
                    "max_results: cap on returned results (default 10). "
                    "backend: search backend name (default 'duckduckgo'). "
                    "Returns: query, backend, results (list of {title, url, snippet})."
                ),
                example={"kind": "web_search", "query": "Claude Code latest news", "max_results": 5},
            ),
        ]

    def _build_ctx(
        self,
        decl: PermissionDecl,
        current_phase: str,
        default_sandbox_policy: dict | None = None,
        compact_now=None,
    ) -> OpContext:
        """Construct the OpContext for a single dispatch iteration."""
        return OpContext(
            workspace=self.workspace,
            events=self.events,
            permission_decl=decl,
            permission_resolver=self._perm,
            skill_name=self._skill_name,
            skill=None,  # control IR doesn't lean on preloaded sub-skills
            # #1672: the control_ir purpose class follows config (was hardcoded
            # "standard"); byte-identical at default (config.model defaults to
            # "standard"). Resolver is config-aware (class_for_purpose).
            model=self._resolver.class_for_purpose("control_ir"),
            resolver=self._resolver,
            subscribers=self.events.subscribers,
            output_language=None,
            max_phase_visits=self._max_phase_visits,
            sub_state_dir_override=None,
            state_dir_strategy="control_ir",
            mcp_servers=self._mcp_servers,
            mcp_clients=self._mcp_clients,
            intervention_bus=self._intervention_bus,
            # #1190 stage (ii): cost recorder for LLM-calling ops (judge_output).
            budget_tracker=self._budget_tracker,
            # #1176 B1: phase on-demand voluntary compaction capability. None
            # for batches with no phase compaction engine wired (compact op
            # then fail-louds compaction_unavailable, same as chat).
            compact_now=compact_now,
            current_phase=current_phase,
            caller=self._caller,
            # R-D13: propagate the running skill's run_id so nested
            # ``run_skill`` invocations can stamp ``parent_run_id`` on
            # the child skill's snapshot.
            parent_skill_run_id=self._skill_run_id,
            # FP-0021: thread the OSRuntime run_id into every OpContext
            # so event emit helpers can stamp the correct run scope.
            run_id=self._run_id,
            # FP-0016 E: pick up agent_id from the EventLog (= populated
            # at session level) so X-Reyn-Agent-Id is added to outgoing
            # MCP HTTP calls dispatched from control-IR ops.
            agent_id=getattr(self.events, "agent_id", None),
            # FP-0017 follow-up: declarative sandbox config (reyn.yaml).
            sandbox_config=self._sandbox_config,
            # FP-0008 #1115 Stage 2: per-run injected exec backend instance
            # (dual-Protocol container backend); None → platform auto-detect.
            sandbox_backend=self._sandbox_backend,
            # FP-0008 #1115 Stage 2 (D): phase-level default SandboxPolicy
            # (frontmatter); sandboxed_exec applies it phase-default-wins.
            default_sandbox_policy=default_sandbox_policy,
            # Issue #364 — multi-modal cluster media-size gate.
            multimodal_config=self._multimodal_config,
            # Issue #383 PR-C — media + tool-result file storage.
            media_store=self._media_store,
            # FP-0016 D: per-skill credential scoping.
            secret_store=self._secret_store,
        )

    async def teardown_mcp_clients(self) -> None:
        """Close all cached MCP clients in the **same asyncio task** as the caller.

        Must be called from the same task that ran ``execute()``.  Calling
        ``close()`` here — rather than letting the ``AsyncExitStack`` be
        finalised by the GC — prevents anyio cancel-scope task-affinity
        violations (G11 hypothesis A+B): the stack's context managers
        (``stdio_client`` / ``ClientSession``) were entered in this task and
        must be exited in this task.
        """
        import logging
        _log = logging.getLogger(__name__)
        clients = list(self._mcp_clients.items())
        self._mcp_clients.clear()
        for name, client in clients:
            try:
                await client.close()
            except Exception as exc:  # noqa: BLE001
                _log.warning("MCP client %s close error: %s", name, exc)

    async def execute(
        self,
        ops: list[ControlIROp],
        phase: str = "",
        decl: PermissionDecl | None = None,
        allowed_ops: set[str] | None = None,
        default_sandbox_policy: dict | None = None,
        compact_now=None,
    ) -> list[dict[str, Any]]:
        """Execute a list of Control IR operations.

        Returns one result dict per op; never raises (errors land in dicts).
        `allowed_ops` filters at the frontend level: ops whose kind is not in
        the set are skipped with `not_allowed_in_phase`. This is defense-in-
        depth against the LLM emitting an op it wasn't shown.

        Each op is wrapped with dispatch_tool (PR37 wave 2C), adding
        tool_called / tool_returned / tool_failed event brackets around
        the existing tool_executed events emitted by op_runtime handlers.
        """
        effective_decl = decl or PermissionDecl()
        ctx = self._build_ctx(effective_decl, phase, default_sandbox_policy, compact_now=compact_now)
        results: list[dict[str, Any]] = []

        # Build a tool catalog for dispatch_tool name/arg validation.
        # Use allowed_ops if provided; fall back to all known op kinds.
        if allowed_ops is not None:
            catalog_ops = allowed_ops
        else:
            from reyn.op_runtime.registry import OP_KIND_MODEL_MAP
            catalog_ops = set(OP_KIND_MODEL_MAP.keys())
        tool_catalog = _build_phase_tool_catalog(catalog_ops)

        caller_id = f"{self._skill_name}.{phase}" if self._skill_name else phase

        dctx = DispatchContext(
            caller_kind="skill_phase",
            caller_id=caller_id,
            chain_id=self._chain_id,
            tool_catalog=tool_catalog,
            events=self.events,
            state_log=self._state_log,
            skill_run_id=self._skill_run_id,
            phase=phase or None,
            resume_plan=self._resume_plan,
        )

        # Build the registry once per execute() call (cheap; cached if needed).
        from reyn.tools import get_default_registry
        from reyn.tools.dispatch import invoke_tool
        from reyn.tools.types import PhaseCallerState, ToolContext
        _registry = get_default_registry()

        # Lazy import to avoid module-init cycles.
        from reyn.op_runtime.registry import is_op_instance_allowed

        for op_idx, op in enumerate(ops):
            if allowed_ops is not None and not is_op_instance_allowed(op, allowed_ops):
                self.events.emit(
                    "control_ir_skipped",
                    kind=op.kind, reason="not_allowed_in_phase",
                )
                results.append({
                    "kind": op.kind,
                    "status": "skipped",
                    "reason": "not_allowed_in_phase",
                })
                continue

            # Exclude None so unset optional fields are OMITTED rather than sent
            # as JSON null. Fine-grained ToolDefinition schemas (read_file etc.)
            # type optional fields strictly (e.g. offset: integer), and a model
            # default of None dumps to null which fails strict validation. Omit
            # matches how the chat LLM emits these args (absent when unset).
            op_args = op.model_dump(exclude={"kind"}, exclude_none=True)

            async def _invoker(args: dict, _op=op, _ctx=ctx, _name=op.kind) -> Any:
                # ADR-0026 Phase 4 step 2: dispatch via the unified
                # ToolRegistry when the op kind has a phase=allow entry.
                # This routes through the canonical handler in
                # src/reyn/tools/<name>.py which itself delegates to
                # op_runtime/<kind>.py (= shared implementation).
                # The legacy execute_op fallback below is retained as a
                # safety net for RAG / internal op kinds whose registry
                # entry isn't wired with phase=allow.
                tool_def = _registry.lookup(_name)
                if tool_def is not None and tool_def.gates.phase == "allow":
                    phase_state = PhaseCallerState(
                        skill_run_id=self._skill_run_id,
                        phase_name=phase or None,
                        op_context=_ctx,
                    )
                    tool_ctx = ToolContext(
                        events=self.events,
                        permission_resolver=self._perm,
                        workspace=self.workspace,
                        caller_kind="phase",
                        phase_state=phase_state,
                        # #1673: thread the config-aware resolver so a tool handler
                        # that spawns a sub-run hands it a real resolver (not None →
                        # literal "standard" → litellm BadRequestError).
                        resolver=self._resolver,
                    )
                    result = await invoke_tool(_registry, _name, args, tool_ctx)
                else:
                    result = await execute_op(_op, _ctx, caller="control_ir")

                if isinstance(result, dict):
                    result.pop("_token_usage", None)
                    if result.get("status") == "denied":
                        raise PermissionError(result.get("error", "permission denied"))
                return result

            # op_invocation_id scopes the WAL step events to a phase-relative
            # sequence number. Combined with ``run_id`` and the WAL ``seq``,
            # forward-replay can disambiguate retries / repeated visits.
            # Format: ``<phase>.<index>`` — index resets per execute() call.
            op_invocation_id = f"{phase or 'phase'}.{op_idx}"
            dispatch_result = await dispatch_tool(
                name=op.kind,
                args=op_args,
                ctx=dctx,
                invoker=_invoker,
                op_invocation_id=op_invocation_id,
            )

            if dispatch_result["status"] == "ok":
                op_result = dispatch_result["data"]
            else:
                # Uniform error shape → internal shape so downstream logic
                # (force_decide / retry) continues to work.
                err = dispatch_result.get("error", {})
                op_result = {
                    "kind": op.kind,
                    "status": "error",
                    "error": err.get("message", str(err)),
                }

            results.append(op_result)

        return results
