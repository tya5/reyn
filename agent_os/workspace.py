from pathlib import Path
from .models import ControlIROp
from .events import EventLog


class Workspace:
    def __init__(self, base_dir: str | Path, events: EventLog) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._events = events
        self.artifacts: list[dict] = []

    def execute_control_ir(self, ops: list[ControlIROp]) -> dict[str, str]:
        results: dict[str, str] = {}
        for op in ops:
            path = self.base_dir / op.path
            if op.op == "write_file":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(op.content or "", encoding="utf-8")
                self._events.emit("tool_executed", op="write_file", path=str(path))
                self._events.emit("workspace_updated", path=str(path))
                results[op.path] = "written"
            elif op.op == "read_file":
                content = path.read_text(encoding="utf-8") if path.exists() else ""
                self._events.emit("tool_executed", op="read_file", path=str(path))
                results[op.path] = content
        return results

    def store_artifact(self, phase: str, artifact: dict) -> None:
        self.artifacts.append({"phase": phase, "artifact": artifact})
        artifact_type = artifact.get("type")
        # data is always present in normalized artifacts; fall back to top-level for safety
        inner = artifact.get("data", artifact)
        keys = list(inner.keys()) if isinstance(inner, dict) else []
        self._events.emit(
            "artifact_created",
            phase=phase,
            artifact_type=artifact_type,
            keys=keys,
        )
