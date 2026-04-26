from pathlib import Path
from .events import EventLog


class Workspace:
    def __init__(self, base_dir: str | Path, events: EventLog) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._events = events
        self.artifacts: list[dict] = []

    def _resolve_path(self, rel_path: str) -> Path:
        """Resolve a workspace-relative path, rejecting any path traversal attempt."""
        path = (self.base_dir / rel_path).resolve()
        if not path.is_relative_to(self.base_dir):
            raise ValueError(f"path escapes workspace: {rel_path!r}")
        return path

    def read_file(self, rel_path: str) -> tuple[str, bool]:
        """Read a file from the workspace. Returns (content, found)."""
        path = self._resolve_path(rel_path)
        if path.exists():
            return path.read_text(encoding="utf-8"), True
        return "", False

    def write_file(self, rel_path: str, content: str) -> None:
        """Write a file into the workspace."""
        path = self._resolve_path(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self._events.emit("workspace_updated", path=str(path))

    def store_artifact(self, phase: str, artifact: dict) -> None:
        self.artifacts.append({"phase": phase, "artifact": artifact})
        artifact_type = artifact.get("type")
        inner = artifact.get("data", artifact)
        keys = list(inner.keys()) if isinstance(inner, dict) else []
        self._events.emit(
            "artifact_created",
            phase=phase,
            artifact_type=artifact_type,
            keys=keys,
        )
