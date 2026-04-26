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
from typing import Any

from .models import AskUserIROp, ControlIROp, ControlIROpSpec, FileIROp
from .workspace import Workspace
from .events import EventLog


class ControlIRExecutor:
    def __init__(self, workspace: Workspace, events: EventLog) -> None:
        self.workspace = workspace
        self.events = events
        self._pending_user_responses: list[dict[str, Any]] = []

    def available_ops(self) -> list[ControlIROpSpec]:
        """Return the Control IR op kinds this executor can handle."""
        return [
            ControlIROpSpec(
                kind="file",
                description=(
                    "Read or write a file inside the workspace. "
                    "Use op='write' to create/overwrite a file; op='read' to retrieve its content."
                ),
                example={"kind": "file", "op": "write", "path": "dir/file.txt", "content": "..."},
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
        ]

    def execute(self, ops: list[ControlIROp], phase: str = "") -> list[dict[str, Any]]:
        """
        Execute a list of Control IR operations.
        Returns a result dict per op; never raises — errors are captured in results.
        ask_user responses are stored and retrieved via pop_user_responses().
        """
        results: list[dict[str, Any]] = []
        for op in ops:
            try:
                if op.kind == "file":
                    result = self._execute_file(op)  # type: ignore[arg-type]
                elif op.kind == "ask_user":
                    result = self._execute_ask_user(op, phase)  # type: ignore[arg-type]
                else:
                    result = {
                        "kind": op.kind,
                        "status": "skipped",
                        "reason": "handler_not_implemented",
                    }
                    self.events.emit("control_ir_skipped", kind=op.kind)
            except Exception as exc:
                kind = getattr(op, "kind", "unknown")
                result = {"kind": kind, "status": "error", "error": str(exc)}
                self.events.emit("control_ir_failed", kind=kind, error=str(exc))
            results.append(result)
        return results

    def pop_user_responses(self) -> list[dict[str, Any]]:
        """Drain and return any ask_user responses collected during the last execute() call."""
        responses = self._pending_user_responses[:]
        self._pending_user_responses = []
        return responses

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

        raise ValueError(f"unsupported file op: {op.op!r}")

    def _execute_ask_user(self, op: AskUserIROp, phase: str) -> dict[str, Any]:
        self.events.emit("user_intervention_requested", phase=phase, question=op.question)

        print(f"\n[ask_user] {op.question}")
        if op.suggestions:
            suggestions_str = " / ".join(f'"{s}"' for s in op.suggestions)
            print(f"  Suggestions: {suggestions_str}")
        print("  > ", end="", flush=True)

        text = input().strip()
        if not text and not op.required:
            text = ""

        self.events.emit("user_intervention_received", phase=phase, answer=text)
        response = {"kind": "ask_user", "question": op.question, "answer": text, "status": "ok"}
        self._pending_user_responses.append(response)
        return response
