"""
Control IR Executor

Executes Control IR operations dispatched by the OS Runtime.
Responsibility: translate ControlIROp instructions into side effects.

Workspace owns data; this executor owns execution.

Currently implemented:
  file — read/write files inside the workspace

Safely skipped (handler_not_implemented):
  tool, mcp, subagent
"""
from __future__ import annotations
from typing import Any

from .models import ControlIROp, ControlIROpSpec, FileIROp
from .workspace import Workspace
from .events import EventLog


class ControlIRExecutor:
    def __init__(self, workspace: Workspace, events: EventLog) -> None:
        self.workspace = workspace
        self.events = events

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
        ]

    def execute(self, ops: list[ControlIROp]) -> list[dict[str, Any]]:
        """
        Execute a list of Control IR operations.
        Returns a result dict per op; never raises — errors are captured in results.
        """
        results: list[dict[str, Any]] = []
        for op in ops:
            try:
                if op.kind == "file":
                    result = self._execute_file(op)  # type: ignore[arg-type]
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
