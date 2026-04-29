"""
Control IR Executor

Executes Control IR operations dispatched by the OS Runtime.
Responsibility: translate ControlIROp instructions into side effects.

Workspace owns data; this executor owns execution.

Currently implemented:
  file     — read/write files inside the workspace
  ask_user — pause phase, ask user a question, collect response

Safely skipped (handler_not_implemented):
  tool, mcp, subagent
"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Callable

from .models import AskUserIROp, ControlIROp, ControlIROpSpec, FileIROp, LintIROp, MCPIROp, RunSkillIROp, ShellIROp, ToolIROp, WebFetchIROp
from .workspace import Workspace
from .events import EventLog
from .model_resolver import ModelResolver
from .permissions import PermissionDecl, PermissionResolver


def _default_user_input(question: str, suggestions: list[str]) -> str:
    print("  > ", end="", flush=True)
    return input().strip()


class ControlIRExecutor:
    def __init__(
        self,
        workspace: Workspace,
        events: EventLog,
        user_input_fn: Callable[[str, list[str]], str] | None = None,
        shell_allowed: bool = False,
        resolver: ModelResolver | None = None,
        permission_resolver: PermissionResolver | None = None,
        max_phase_visits: int = 25,
        skill_name: str = "",
        mcp_servers: dict | None = None,
    ) -> None:
        self.workspace = workspace
        self.events = events
        self._user_input_fn = user_input_fn or _default_user_input
        self._max_phase_visits = max_phase_visits
        self._shell_allowed = shell_allowed
        self._resolver = resolver or ModelResolver({})
        self._perm = permission_resolver
        self._skill_name = skill_name
        self._mcp_servers: dict = (mcp_servers or {}).get("servers", {})
        self._mcp_clients: dict = {}  # server_name → MCPHTTPClient (cached per run)

    def available_ops(self) -> list[ControlIROpSpec]:
        """Return the Control IR op kinds this executor can handle."""
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
        ]

    def execute(
        self,
        ops: list[ControlIROp],
        phase: str = "",
        decl: PermissionDecl | None = None,
    ) -> list[dict[str, Any]]:
        """
        Execute a list of Control IR operations.
        Returns a result dict per op; never raises — errors are captured in results.
        Content-bearing results (file reads, ask_user answers) are returned directly
        so the caller can feed them back to the LLM.
        """
        effective_decl = decl or PermissionDecl()
        results: list[dict[str, Any]] = []
        for op in ops:
            try:
                if op.kind == "file":
                    if self._perm and getattr(op, "op", None) in ("write", "edit", "delete"):
                        self._perm.require_file_write(effective_decl, op.path, self._skill_name)  # type: ignore[union-attr]
                    result = self._execute_file(op)  # type: ignore[arg-type]
                elif op.kind == "ask_user":
                    result = self._execute_ask_user(op, phase)  # type: ignore[arg-type]
                elif op.kind == "shell":
                    if self._perm:
                        self._perm.require_shell(effective_decl, getattr(op, "cmd", ""))
                    elif not self._shell_allowed:
                        result = {"kind": "shell", "status": "skipped", "reason": "shell_not_allowed"}
                        self.events.emit("control_ir_skipped", kind="shell", reason="shell_not_allowed")
                        results.append(result)
                        continue
                    result = self._execute_shell(op)  # type: ignore[arg-type]
                elif op.kind == "mcp":
                    if self._perm:
                        self._perm.require_mcp(effective_decl, getattr(op, "server", ""))
                    result = self._execute_mcp(op)  # type: ignore[arg-type]
                elif op.kind == "tool":
                    if self._perm:
                        self._perm.require_tool(effective_decl, getattr(op, "name", ""))
                    result = {"kind": "tool", "status": "skipped", "reason": "handler_not_implemented"}
                    self.events.emit("control_ir_skipped", kind="tool")
                elif op.kind == "lint":
                    result = self._execute_lint(op)  # type: ignore[arg-type]
                elif op.kind == "run_skill":
                    result = self._execute_run_skill(op)  # type: ignore[arg-type]
                elif op.kind == "web_fetch":
                    result = self._execute_web_fetch(op)  # type: ignore[arg-type]
                else:
                    result = {
                        "kind": op.kind,
                        "status": "skipped",
                        "reason": "handler_not_implemented",
                    }
                    self.events.emit("control_ir_skipped", kind=op.kind)
            except PermissionError as exc:
                kind = getattr(op, "kind", "unknown")
                path = getattr(op, "path", None)
                result = {"kind": kind, "status": "denied", "error": str(exc)}
                self.events.emit("permission_denied", kind=kind, path=path, reason=str(exc))
            except Exception as exc:
                kind = getattr(op, "kind", "unknown")
                result = {"kind": kind, "status": "error", "error": str(exc)}
                self.events.emit("control_ir_failed", kind=kind, error=str(exc))
            results.append(result)
        return results

    def _execute_file(self, op: FileIROp) -> dict[str, Any]:
        if op.op == "write":
            self.workspace.write_file(op.path, op.content or "")
            self.events.emit("tool_executed", op="write_file", path=op.path)
            return {"kind": "file", "op": "write", "path": op.path, "status": "ok"}

        if op.op == "read":
            content, found = self.workspace.read_file(op.path)
            if found and (op.offset is not None or op.limit is not None):
                lines = content.splitlines(keepends=True)
                start = op.offset or 0
                sliced = lines[start:start + op.limit] if op.limit is not None else lines[start:]
                content = "".join(sliced)
            self.events.emit("tool_executed", op="read_file", path=op.path)
            return {
                "kind": "file",
                "op": "read",
                "path": op.path,
                "status": "ok" if found else "not_found",
                "content": content,
            }

        if op.op == "glob":
            matches = self.workspace.glob_files(op.path, max_results=op.max_results)
            self.events.emit("tool_executed", op="glob_files", path=op.path, match_count=len(matches))
            return {
                "kind": "file",
                "op": "glob",
                "pattern": op.path,
                "status": "ok",
                "matches": matches,
                "count": len(matches),
            }

        if op.op == "delete":
            deleted = self.workspace.delete_file(op.path)
            self.events.emit("tool_executed", op="delete_file", path=op.path, deleted=deleted)
            return {"kind": "file", "op": "delete", "path": op.path, "status": "ok", "deleted": deleted}

        if op.op == "grep":
            return self._execute_grep(op)

        if op.op == "edit":
            return self._execute_edit(op)

        raise ValueError(f"unsupported file op: {op.op!r}")

    def _execute_grep(self, op: FileIROp) -> dict[str, Any]:
        import re
        from pathlib import Path

        if not op.pattern:
            return {"kind": "file", "op": "grep", "status": "error", "error": "pattern is required for grep"}
        flags = re.IGNORECASE if op.case_insensitive else 0
        try:
            regex = re.compile(op.pattern, flags)
        except re.error as exc:
            return {"kind": "file", "op": "grep", "status": "error", "error": f"invalid regex: {exc}"}

        search_root = Path(op.path) if op.path else Path(".")
        try:
            resolved_root = self.workspace._resolve_read(str(search_root))
        except PermissionError as exc:
            return {"kind": "file", "op": "grep", "status": "denied", "error": str(exc)}

        # Collect candidate files
        if resolved_root.is_file():
            candidates = [resolved_root]
        else:
            glob_pattern = op.glob or "**/*"
            candidates = sorted(f for f in resolved_root.glob(glob_pattern) if f.is_file())
        if op.file_type:
            ext = op.file_type.lstrip(".")
            candidates = [f for f in candidates if f.suffix.lstrip(".") == ext]

        def _rel(p: Path) -> str:
            try:
                return str(p.relative_to(self.workspace.base_dir))
            except ValueError:
                return str(p)

        if op.output_mode == "files_with_matches":
            matched: list[str] = []
            for f in candidates:
                try:
                    if regex.search(f.read_text(encoding="utf-8", errors="replace")):
                        matched.append(_rel(f))
                except OSError:
                    continue
            self.events.emit("tool_executed", op="grep", pattern=op.pattern, match_count=len(matched))
            return {"kind": "file", "op": "grep", "status": "ok",
                    "output_mode": "files_with_matches", "files": matched, "count": len(matched)}

        if op.output_mode == "count":
            total = 0
            for f in candidates:
                try:
                    total += len(regex.findall(f.read_text(encoding="utf-8", errors="replace")))
                except OSError:
                    continue
            self.events.emit("tool_executed", op="grep", pattern=op.pattern, match_count=total)
            return {"kind": "file", "op": "grep", "status": "ok",
                    "output_mode": "count", "count": total}

        # output_mode == "content"
        matches: list[dict] = []
        head_limit = op.head_limit
        done = False
        for f in candidates:
            if done:
                break
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            rel = _rel(f)
            for i, line in enumerate(lines):
                if not regex.search(line):
                    continue
                entry: dict[str, Any] = {"path": rel, "line_number": i + 1, "content": line}
                if op.context_before or op.context_after:
                    start = max(0, i - op.context_before)
                    end = min(len(lines), i + op.context_after + 1)
                    entry["context"] = [
                        {"line_number": j + 1, "content": lines[j], "is_match": j == i}
                        for j in range(start, end)
                    ]
                matches.append(entry)
                if head_limit is not None and len(matches) >= head_limit:
                    done = True
                    break

        self.events.emit("tool_executed", op="grep", pattern=op.pattern, match_count=len(matches))
        return {"kind": "file", "op": "grep", "status": "ok",
                "output_mode": "content", "pattern": op.pattern,
                "matches": matches, "count": len(matches)}

    def _execute_edit(self, op: FileIROp) -> dict[str, Any]:
        if op.old_string is None:
            return {"kind": "file", "op": "edit", "status": "error", "error": "old_string is required"}
        if op.new_string is None:
            return {"kind": "file", "op": "edit", "status": "error", "error": "new_string is required"}

        content, found = self.workspace.read_file(op.path)
        if not found:
            return {"kind": "file", "op": "edit", "status": "not_found", "path": op.path}

        count = content.count(op.old_string)
        if count == 0:
            return {"kind": "file", "op": "edit", "status": "error",
                    "error": "old_string not found in file"}
        if not op.replace_all and count > 1:
            return {"kind": "file", "op": "edit", "status": "error",
                    "error": f"old_string appears {count} times; set replace_all=true to replace all occurrences"}

        new_content = content.replace(op.old_string, op.new_string) if op.replace_all \
            else content.replace(op.old_string, op.new_string, 1)
        self.workspace.write_file(op.path, new_content)
        replacements = count if op.replace_all else 1
        self.events.emit("tool_executed", op="edit_file", path=op.path, replacements=replacements)
        return {"kind": "file", "op": "edit", "path": op.path, "status": "ok", "replacements": replacements}

    def _execute_ask_user(self, op: AskUserIROp, phase: str) -> dict[str, Any]:
        self.events.emit(
            "user_intervention_requested",
            phase=phase,
            question=op.question,
            suggestions=op.suggestions or [],
        )

        text = self._user_input_fn(op.question, op.suggestions or [])
        if not text and not op.required:
            text = ""

        self.events.emit("user_intervention_received", phase=phase, answer=text)
        return {"kind": "ask_user", "question": op.question, "answer": text, "status": "ok"}

    def _execute_lint(self, op: LintIROp) -> dict[str, Any]:
        from .compiler.linter import lint_skill_dir
        skill_dir = Path(op.skill_path)
        if not (skill_dir / "skill.md").exists():
            return {
                "kind": "lint",
                "status": "error",
                "skill_path": op.skill_path,
                "passed": False,
                "error_count": 1,
                "warning_count": 0,
                "issues": [f"[ERROR] skill.md not found at '{op.skill_path}'"],
            }
        issues = lint_skill_dir(skill_dir)
        error_count = sum(1 for i in issues if i.severity == "error")
        warning_count = sum(1 for i in issues if i.severity == "warning")
        self.events.emit(
            "lint_completed",
            skill_path=op.skill_path,
            error_count=error_count,
            warning_count=warning_count,
        )
        return {
            "kind": "lint",
            "status": "ok",
            "skill_path": op.skill_path,
            "passed": error_count == 0,
            "error_count": error_count,
            "warning_count": warning_count,
            "issues": [str(i) for i in issues],
        }

    def _execute_run_skill(self, op: RunSkillIROp) -> dict[str, Any]:
        from .compiler import load_dsl_skill
        from .sub_skill_runner import invoke_sub_skill

        # Resolve app name or path
        skill_ref = op.skill
        if "/" not in skill_ref and not skill_ref.endswith(".md"):
            from reyn.skill_paths import resolve_skill_path
            skill_dir, inferred_root = resolve_skill_path(skill_ref)
            skill_md_path = str(skill_dir / "skill.md")
            dsl_root = str(inferred_root) if inferred_root else None
        else:
            skill_md_path = skill_ref
            dsl_root = None

        sub_skill = load_dsl_skill(skill_md_path, dsl_root=dsl_root)
        model = op.model or "standard"

        # Sub state_dir: isolated under parent state_dir/invoke/ or shared
        safe_name = skill_ref.replace("/", "_").replace(".", "_")
        parent_state = self.workspace.state_dir
        if op.workspace == "shared":
            sub_state_dir = str(parent_state)
        else:
            sub_state_dir = str(parent_state / "invoke" / safe_name)

        self.events.emit("run_skill_started", skill=op.skill, state_dir=sub_state_dir)

        run_result = invoke_sub_skill(
            sub_skill, op.input,
            model=model,
            state_dir=sub_state_dir,
            subscribers=self.events.subscribers,
            resolver=self._resolver,
            output_language=op.output_language,
            max_phase_visits=self._max_phase_visits,
        )

        # Glob paths for events and artifacts (state_dir-relative)
        sub_state = Path(sub_state_dir)
        parent_state_path = self.workspace.state_dir
        try:
            rel = sub_state.relative_to(parent_state_path)
            events_glob = str(rel / "runs" / "*.jsonl")
            artifacts_glob = str(rel / "artifacts" / "**" / "*.json")
        except ValueError:
            events_glob = str(sub_state / "runs" / "*.jsonl")
            artifacts_glob = str(sub_state / "artifacts" / "**" / "*.json")

        usage = run_result.token_usage
        self.events.emit(
            "run_skill_completed",
            skill=op.skill,
            status=run_result.status,
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
        )
        return {
            "kind": "run_skill",
            "status": run_result.status,
            "skill": op.skill,
            "success": run_result.ok,
            "final_output": run_result.data,
            "phase_artifacts": run_result.phase_artifacts,
            "events_glob": events_glob,
            "artifacts_glob": artifacts_glob,
            "workspace": sub_state_dir,
        }

    def _execute_mcp(self, op: MCPIROp) -> dict[str, Any]:
        from .mcp_client import MCPHTTPClient, MCPError, expand_env

        server_cfg = self._mcp_servers.get(op.server)
        if not server_cfg:
            return {
                "kind": "mcp", "status": "error",
                "error": f"MCP server '{op.server}' is not configured. "
                         f"Add it under mcp.servers in reyn.yaml or .reyn/config.yaml.",
            }

        expanded = expand_env(server_cfg)
        url = expanded.get("url", "")
        if not url:
            return {"kind": "mcp", "status": "error",
                    "error": f"MCP server '{op.server}' has no url configured."}

        headers = {str(k): str(v) for k, v in (expanded.get("headers") or {}).items()}

        if op.server not in self._mcp_clients:
            self._mcp_clients[op.server] = MCPHTTPClient(url, headers)
        client = self._mcp_clients[op.server]

        self.events.emit("mcp_called", server=op.server, tool=op.tool, args=op.args)
        try:
            result = client.call_tool(op.tool, op.args)
        except MCPError as exc:
            self.events.emit("mcp_failed", server=op.server, tool=op.tool, error=str(exc))
            return {"kind": "mcp", "status": "error", "server": op.server,
                    "tool": op.tool, "error": str(exc)}

        # Flatten MCP content array to a single text string for the LLM
        content_items = result.get("content", [])
        if isinstance(content_items, list):
            text = "\n".join(
                item.get("text", "") for item in content_items
                if isinstance(item, dict) and item.get("type") == "text"
            )
        else:
            text = str(content_items)

        is_error = bool(result.get("isError"))
        self.events.emit("mcp_completed", server=op.server, tool=op.tool, is_error=is_error)
        return {
            "kind": "mcp",
            "status": "error" if is_error else "ok",
            "server": op.server,
            "tool": op.tool,
            "content": text,
            "raw": result,
        }

    def _execute_web_fetch(self, op: WebFetchIROp) -> dict[str, Any]:
        import html
        import html.parser
        import httpx

        class _TextExtractor(html.parser.HTMLParser):
            _SKIP = {"script", "style", "head", "noscript", "svg", "iframe"}

            def __init__(self) -> None:
                super().__init__()
                self._parts: list[str] = []
                self._skip_depth = 0

            def handle_starttag(self, tag: str, attrs: object) -> None:
                if tag in self._SKIP:
                    self._skip_depth += 1

            def handle_endtag(self, tag: str) -> None:
                if tag in self._SKIP and self._skip_depth > 0:
                    self._skip_depth -= 1

            def handle_data(self, data: str) -> None:
                if self._skip_depth == 0:
                    stripped = data.strip()
                    if stripped:
                        self._parts.append(stripped)

            def text(self) -> str:
                return "\n".join(self._parts)

        self.events.emit("web_fetch_started", url=op.url)
        try:
            response = httpx.get(
                op.url,
                timeout=op.timeout,
                follow_redirects=True,
                headers={"User-Agent": "reyn/1.0"},
            )
        except httpx.TimeoutException:
            return {"kind": "web_fetch", "url": op.url, "status": "timeout",
                    "error": f"request timed out after {op.timeout}s"}
        except httpx.RequestError as exc:
            return {"kind": "web_fetch", "url": op.url, "status": "error", "error": str(exc)}

        content_type = response.headers.get("content-type", "")
        raw = response.text

        if "text/html" in content_type:
            extractor = _TextExtractor()
            extractor.feed(raw)
            content = extractor.text()
        else:
            content = raw

        truncated = len(content) > op.max_length
        if truncated:
            content = content[: op.max_length]

        self.events.emit(
            "web_fetch_completed",
            url=op.url,
            status_code=response.status_code,
            content_length=len(content),
            truncated=truncated,
        )
        return {
            "kind": "web_fetch",
            "url": op.url,
            "status": "ok",
            "status_code": response.status_code,
            "content_type": content_type,
            "content": content,
            "truncated": truncated,
        }

    def _execute_shell(self, op: ShellIROp) -> dict[str, Any]:
        import subprocess
        self.events.emit("shell_started", cmd=op.cmd, timeout=op.timeout)
        try:
            proc = subprocess.run(
                op.cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=op.timeout,
            )
            self.events.emit(
                "shell_completed",
                cmd=op.cmd,
                returncode=proc.returncode,
                stdout_len=len(proc.stdout),
                stderr_len=len(proc.stderr),
            )
            return {
                "kind": "shell",
                "status": "ok" if proc.returncode == 0 else "error",
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        except subprocess.TimeoutExpired:
            self.events.emit("shell_timeout", cmd=op.cmd, timeout=op.timeout)
            return {
                "kind": "shell",
                "status": "timeout",
                "returncode": -1,
                "stdout": "",
                "stderr": f"Command timed out after {op.timeout}s",
            }
