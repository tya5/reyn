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

from .models import AskUserIROp, ControlIROp, ControlIROpSpec, EvalIROp, FileIROp, LintIROp, MCPIROp, RunAppIROp, ShellIROp, ToolIROp
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
    ) -> None:
        self.workspace = workspace
        self.events = events
        self._user_input_fn = user_input_fn or _default_user_input
        self._shell_allowed = shell_allowed
        self._resolver = resolver or ModelResolver({})
        self._perm = permission_resolver

    def available_ops(self) -> list[ControlIROpSpec]:
        """Return the Control IR op kinds this executor can handle."""
        return [
            ControlIROpSpec(
                kind="file",
                description=(
                    "Read, write, or glob files in the project. "
                    "All paths are relative to the project root (CWD). "
                    "op='write': create or overwrite a file. "
                    "op='read': retrieve a single file's content. "
                    "op='glob': expand a glob pattern (supports ** for recursive) and return "
                    "matching file paths; use this to discover files before reading them. "
                    "max_results (default 50) caps glob output. "
                    "op='delete': delete a single file (no-op if not found)."
                ),
                example={"kind": "file", "op": "glob", "path": "src/**/*.py"},
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
                example={"kind": "shell", "cmd": "reyn run ...pp-dsl dsl/apps/foo/app.md --input 'hello'", "timeout": 120},
            )] if self._shell_allowed else []),
            ControlIROpSpec(
                kind="lint",
                description=(
                    "Run the DSL linter against a directory and return issues. "
                    "dsl_root: project-relative path to the DSL root directory (default: 'reyn/'). "
                    "Returns: passed (bool), error_count, warning_count, issues (list of strings)."
                ),
                example={"kind": "lint", "dsl_root": "reyn/"},
            ),
            ControlIROpSpec(
                kind="eval",
                description=(
                    "Run an eval spec against its target app and return scores. "
                    "spec_path: project-relative path to the eval.md file. "
                    "model: model class name (e.g. 'standard') or LiteLLM string for running the target app. "
                    "judge_model: model for LLM-as-judge (defaults to model). "
                    "Returns: passed (bool), overall_score, passed_criteria, total_criteria, weakest_phase, cases."
                ),
                example={"kind": "eval", "spec_path": "eval_specs/my_app/eval.md", "model": "standard"},
            ),
            ControlIROpSpec(
                kind="run_app",
                description=(
                    "Run a reyn app in-process and return its final output. "
                    "app: app name (resolved via search path) or path to app.md. "
                    "input: input artifact dict to pass to the sub-app. "
                    "model: model class or LiteLLM string (default: inherit from runtime). "
                    "workspace: 'isolated' (default) creates a sub-workspace; 'shared' uses the current workspace. "
                    "Returns: status ('finished'|'loop_limit_exceeded'), final_output (dict), "
                    "token_usage (prompt_tokens, completion_tokens)."
                ),
                example={"kind": "run_app", "app": "my_app", "input": {"type": "user_message", "data": {"text": "hello"}}},
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
                    result = {"kind": "mcp", "status": "skipped", "reason": "handler_not_implemented"}
                    self.events.emit("control_ir_skipped", kind="mcp")
                elif op.kind == "tool":
                    if self._perm:
                        self._perm.require_tool(effective_decl, getattr(op, "name", ""))
                    result = {"kind": "tool", "status": "skipped", "reason": "handler_not_implemented"}
                    self.events.emit("control_ir_skipped", kind="tool")
                elif op.kind == "lint":
                    result = self._execute_lint(op)  # type: ignore[arg-type]
                elif op.kind == "eval":
                    result = self._execute_eval(op)  # type: ignore[arg-type]
                elif op.kind == "run_app":
                    result = self._execute_run_app(op)  # type: ignore[arg-type]
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

        raise ValueError(f"unsupported file op: {op.op!r}")

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
        from .compiler.linter import lint_dsl
        resolved = self._resolve_dsl_root(Path(op.dsl_root))
        issues = lint_dsl(resolved)
        error_count = sum(1 for i in issues if i.severity == "error")
        warning_count = sum(1 for i in issues if i.severity == "warning")
        self.events.emit(
            "lint_completed",
            dsl_root=op.dsl_root,
            error_count=error_count,
            warning_count=warning_count,
        )
        return {
            "kind": "lint",
            "status": "ok",
            "dsl_root": op.dsl_root,
            "passed": error_count == 0,
            "error_count": error_count,
            "warning_count": warning_count,
            "issues": [str(i) for i in issues],
        }

    @staticmethod
    def _resolve_dsl_root(path: "Path") -> "Path":
        """Walk up from path until finding a directory with an 'apps' subdirectory.

        Handles the case where the LLM provides an app-level path like
        'dsl/apps/my_app' instead of the dsl root 'dsl/'.
        """
        from pathlib import Path
        p = path if path.is_dir() else path.parent
        visited: set[Path] = set()
        while p not in visited:
            if (p / "apps").is_dir():
                return p
            visited.add(p)
            parent = p.parent
            if parent == p:
                break
            p = parent
        return path  # fall back to original if no apps/ found

    def _execute_eval(self, op: EvalIROp) -> dict[str, Any]:
        from datetime import datetime, timezone
        from .compiler.eval_loader import load_eval_spec
        from .compiler import load_dsl_app
        from .eval.runner import EvalRunner
        from .eval.models import EvalRunResult

        # spec_path and app: in eval.md are both CWD-relative (workspace = CWD)
        spec_full_path = Path(op.spec_path)
        spec = load_eval_spec(str(spec_full_path))

        app_path = Path(spec.app_dsl_path)
        dsl_root = Path(spec.dsl_root) if spec.dsl_root else None
        app = load_dsl_app(app_path, dsl_root=dsl_root)

        model = self._resolver.resolve(op.model)
        judge_model = self._resolver.resolve(op.judge_model or op.model)
        eval_state_dir = str(self.workspace.state_dir / "eval_runs")

        runner = EvalRunner(
            spec=spec,
            app=app,
            model=model,
            judge_model=judge_model,
            state_dir=eval_state_dir,
            output_language=op.output_language,
            app_subscribers=[],
        )
        case_results = [runner.run_case(case) for case in spec.cases]

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_result = EvalRunResult(
            spec_path=str(spec_full_path),
            app_name=app.name,
            model=op.model,
            judge_model=judge_model,
            timestamp=ts,
            case_results=case_results,
            cost_summary=runner.build_cost_summary(),
        )
        self.events.emit(
            "eval_completed",
            spec_path=op.spec_path,
            overall_score=run_result.overall_score,
            passed_criteria=run_result.overall_passed,
            total_criteria=run_result.overall_total,
        )
        return {
            "kind": "eval",
            "status": "ok",
            "spec_path": op.spec_path,
            "passed": run_result.overall_score >= 0.6,
            "overall_score": run_result.overall_score,
            "passed_criteria": run_result.overall_passed,
            "total_criteria": run_result.overall_total,
            "weakest_phase": run_result.weakest_phase() or "",
            "case_count": len(case_results),
            "cases": [
                {"name": cr.case_name, "score": cr.score, "passed": cr.passed, "total": cr.total}
                for cr in case_results
            ],
        }

    def _execute_run_app(self, op: RunAppIROp) -> dict[str, Any]:
        from .compiler import load_dsl_app
        from reyn.agent import Agent

        # Resolve app name or path
        app_ref = op.app
        if "/" not in app_ref and not app_ref.endswith(".md"):
            from reyn._cli import _resolve_app_name
            app_dir, inferred_root = _resolve_app_name(app_ref)
            app_path = str(app_dir / "app.md")
            dsl_root = str(inferred_root) if inferred_root else None
        else:
            app_path = app_ref
            dsl_root = None

        sub_app = load_dsl_app(app_path, dsl_root=dsl_root)
        model = op.model or "standard"

        # Sub state_dir: isolated under parent state_dir/invoke/ or shared
        safe_name = app_ref.replace("/", "_").replace(".", "_")
        parent_state = self.workspace.state_dir
        if op.workspace == "shared":
            sub_state_dir = str(parent_state)
        else:
            sub_state_dir = str(parent_state / "invoke" / safe_name)

        self.events.emit("run_app_started", app=op.app, state_dir=sub_state_dir)

        agent = Agent(
            model=model,
            state_dir=sub_state_dir,
            strict=False,
            subscribers=self.events.subscribers,
            resolver=self._resolver,
        )
        run_result = agent.run(sub_app, op.input, output_language=op.output_language)

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
            "run_app_completed",
            app=op.app,
            status=run_result.status,
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
        )
        return {
            "kind": "run_app",
            "status": run_result.status,
            "app": op.app,
            "success": run_result.ok,
            "final_output": run_result.data,
            "events_glob": events_glob,
            "artifacts_glob": artifacts_glob,
            "workspace": sub_workspace_dir,
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
