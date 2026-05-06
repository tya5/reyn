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

from typing import Any

from reyn.dispatch import DispatchContext, dispatch_tool
from reyn.events.events import EventLog
from reyn.llm.model_resolver import ModelResolver
from reyn.op_runtime import execute_op
from reyn.op_runtime.context import OpContext
from reyn.op_runtime.registry import OP_KIND_MODEL_MAP
from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.schemas.models import (
    ControlIROp,
    ControlIROpSpec,
)
from reyn.user_intervention import InterventionBus
from reyn.workspace.workspace import Workspace

# Map: op kind -> IROp Pydantic model (used to derive tool parameter schemas).
# Single source of truth lives in reyn.op_runtime.registry.OP_KIND_MODEL_MAP.
_IROP_MODEL_MAP = OP_KIND_MODEL_MAP


def _build_phase_tool_catalog(allowed_ops: set[str]) -> dict[str, dict]:
    """Build a tool_catalog for dispatch_tool from a set of allowed op kinds.

    Each allowed op kind becomes a tool entry whose parameters schema is
    derived from the corresponding IROp Pydantic model (with 'kind' removed
    from required fields, since kind is implicit at dispatch time).

    Returns a dict[str, dict] in litellm tools= entry shape:
        {op_kind: {"function": {"name": op_kind, "parameters": <json schema>}}}
    """
    catalog: dict[str, dict] = {}
    for kind in allowed_ops:
        model_cls = _IROP_MODEL_MAP.get(kind)
        if model_cls is None:
            # Unknown op kinds get a schema-less entry (no arg validation)
            catalog[kind] = {"function": {"name": kind}}
            continue
        schema = model_cls.model_json_schema()
        # Remove 'kind' from required — it's implicit at dispatch time and
        # would fail validation since we pass args without the kind field.
        required = [f for f in schema.get("required", []) if f != "kind"]
        properties = {k: v for k, v in schema.get("properties", {}).items() if k != "kind"}
        clean_schema = {
            "type": "object",
            "properties": properties,
        }
        if required:
            clean_schema["required"] = required
        catalog[kind] = {
            "function": {
                "name": kind,
                "parameters": clean_schema,
            }
        }
    return catalog


class ControlIRExecutor:
    def __init__(
        self,
        workspace: Workspace,
        events: EventLog,
        intervention_bus: InterventionBus | None = None,
        shell_allowed: bool = False,
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
    ) -> None:
        self.workspace = workspace
        self.events = events
        self._intervention_bus = intervention_bus
        self._max_phase_visits = max_phase_visits
        self._shell_allowed = shell_allowed
        self._resolver = resolver or ModelResolver({})
        self._perm = permission_resolver
        self._skill_name = skill_name
        self._mcp_servers: dict = (mcp_servers or {}).get("servers", {})
        self._mcp_clients: dict = {}  # cached across ops
        self._caller = caller
        self._chain_id = chain_id
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

    def available_ops(self) -> list[ControlIROpSpec]:
        """Return the Control IR op kinds this executor advertises to the LLM.

        These specs flow into ContextFrame.available_control_ops; the LLM picks
        from this list when emitting `control_ir`. Op kinds are filtered per
        runtime config (shell_allowed, mcp_servers configured).
        """
        return [
            ControlIROpSpec(
                kind="file",
                description=(
                    "File operations. All paths are relative to the project root (CWD). "
                    "op='read': read a file. offset (int, 0-indexed line) and limit (int, line count) enable partial reads. "
                    "op='write': create or overwrite a file. content: full file text. "
                    "op='glob': find files by pattern (supports ** for recursive). max_results (default 50) caps output. "
                    "op='delete': delete a single file (no-op if not found). "
                    "op='grep': search file contents with a regex. path=search root (dir or file). "
                    "  pattern: required regex. glob: file filter (e.g. '**/*.py'). file_type: extension filter (e.g. 'py'). "
                    "  output_mode: 'content' (default, returns matches with line numbers), "
                    "  'files_with_matches' (paths only), 'count' (total match count). "
                    "  case_insensitive: bool. context_before/context_after: surrounding lines. head_limit: cap matches. "
                    "op='edit': partial replace in a file. old_string must match exactly once (or use replace_all=true). "
                    "  new_string: replacement text. Fails with error if old_string is not found or not unique."
                ),
                example={"kind": "file", "op": "grep", "path": "src", "pattern": "def \\w+", "glob": "**/*.py", "output_mode": "content"},
            ),
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
            *([ControlIROpSpec(
                kind="shell",
                description=(
                    "Execute a shell command and return stdout, stderr, and returncode. "
                    "cmd: the shell command string. "
                    "timeout: max seconds to wait (default 120). "
                    "Runs in the project root directory. "
                    "Use for running sub-processes such as 'reyn run ...'."
                ),
                example={"kind": "shell", "cmd": "reyn run my_skill 'hello'", "timeout": 120},
            )] if self._shell_allowed else []),
            *([ControlIROpSpec(
                kind="mcp",
                description=(
                    "Call a tool on a configured MCP server (HTTP transport). "
                    "server: the server name as defined in mcp.servers config. "
                    "tool: the tool name exposed by that server. "
                    "args: arguments dict to pass to the tool. "
                    "Returns: content (text), raw (full MCP result). "
                    "Phase must declare permissions.mcp: [server_name] to use this op."
                ),
                example={"kind": "mcp", "server": "my_tool", "tool": "search", "args": {"query": "hello"}},
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
                kind="run_skill",
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
                example={"kind": "run_skill", "skill": "my_skill", "input": {"type": "user_message", "data": {"text": "hello"}}},
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

    def _build_ctx(self, decl: PermissionDecl, current_phase: str) -> OpContext:
        """Construct the OpContext for a single dispatch iteration."""
        return OpContext(
            workspace=self.workspace,
            events=self.events,
            permission_decl=decl,
            permission_resolver=self._perm,
            skill_name=self._skill_name,
            skill=None,  # control IR doesn't lean on preloaded sub-skills
            model="standard",
            resolver=self._resolver,
            subscribers=self.events.subscribers,
            output_language=None,
            max_phase_visits=self._max_phase_visits,
            sub_state_dir_override=None,
            state_dir_strategy="control_ir",
            shell_allowed=self._shell_allowed,
            mcp_servers=self._mcp_servers,
            mcp_clients=self._mcp_clients,
            intervention_bus=self._intervention_bus,
            current_phase=current_phase,
            caller=self._caller,
            # R-D13: propagate the running skill's run_id so nested
            # ``run_skill`` invocations can stamp ``parent_run_id`` on
            # the child skill's snapshot.
            parent_skill_run_id=self._skill_run_id,
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
        ctx = self._build_ctx(effective_decl, phase)
        results: list[dict[str, Any]] = []

        # Build a tool catalog for dispatch_tool name/arg validation.
        # Use allowed_ops if provided; fall back to all known op kinds.
        catalog_ops = allowed_ops if allowed_ops is not None else set(_IROP_MODEL_MAP.keys())
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

        for op_idx, op in enumerate(ops):
            if allowed_ops is not None and op.kind not in allowed_ops:
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

            op_args = op.model_dump(exclude={"kind"})

            async def _invoker(args: dict, _op=op, _ctx=ctx) -> Any:
                # execute_op catches PermissionError internally and returns
                # {"status": "denied"}.  Re-raise as PermissionError so that
                # dispatch_tool's handler emits tool_failed with error_kind
                # "permission_denied" (the designed cross-cutting pattern).
                result = await execute_op(_op, _ctx, caller="control_ir")
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
