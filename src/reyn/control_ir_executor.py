"""
Control IR Executor — dynamic frontend for op_runtime.

Receives a list of ControlIROp instances emitted by the LLM, applies
phase-level allow-list filtering, then delegates each op to op_runtime.
Permission checks, event emission, and the per-op error envelope all
live in op_runtime so the same backend serves the static (preprocessor)
frontend as well.

Workspace owns data; op_runtime owns execution; this module owns
LLM-act-turn dispatch policy.
"""
from __future__ import annotations
from typing import Any

from .models import ControlIROp, ControlIROpSpec
from .workspace import Workspace
from .events import EventLog
from .model_resolver import ModelResolver
from .permissions import PermissionDecl, PermissionResolver
from .op_runtime import execute_op
from .op_runtime.context import OpContext
from .user_intervention import InterventionBus


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
            output_language="ja",
            max_phase_visits=self._max_phase_visits,
            sub_state_dir_override=None,
            state_dir_strategy="control_ir",
            shell_allowed=self._shell_allowed,
            mcp_servers=self._mcp_servers,
            mcp_clients=self._mcp_clients,
            intervention_bus=self._intervention_bus,
            current_phase=current_phase,
            caller=self._caller,
        )

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
        """
        effective_decl = decl or PermissionDecl()
        ctx = self._build_ctx(effective_decl, phase)
        results: list[dict[str, Any]] = []

        for op in ops:
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

            result = await execute_op(op, ctx, caller="control_ir")
            # run_skill emits an internal `_token_usage` field used by the
            # preprocessor frontend; control IR doesn't propagate it, so drop it.
            result.pop("_token_usage", None)
            results.append(result)

        return results
