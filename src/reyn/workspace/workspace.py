from __future__ import annotations
import glob as _glob
import json
from pathlib import Path
from typing import TYPE_CHECKING

from reyn.events.events import EventLog

if TYPE_CHECKING:
    from reyn.permissions.permissions import PermissionResolver


class Workspace:
    """
    Workspace where the agent operates.

    base_dir  : CWD — where relative file paths resolve (read + write).
    state_dir : .reyn/ — where artifacts, event logs, and invoke sub-dirs live.

    Read  policy : any path under base_dir (CWD), plus paths the PermissionResolver
                   has approved for this skill (declared via `permissions.file.read`).
    Write policy : any path under base_dir (CWD), plus paths the PermissionResolver
                   has approved for this skill (declared via `permissions.file.write`).
    """

    def __init__(
        self,
        events: EventLog,
        permission_resolver: "PermissionResolver | None" = None,
        skill_name: str = "",
    ) -> None:
        self.base_dir = Path.cwd()
        self.state_dir = (self.base_dir / ".reyn").resolve()
        self._events = events
        self.artifacts: list[dict] = []
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "artifacts").mkdir(exist_ok=True)
        self._perm = permission_resolver
        self._skill_name = skill_name

    def _resolve_read(self, path_str: str) -> Path:
        p = Path(path_str).expanduser()
        resolved = (self.base_dir / p).resolve() if not p.is_absolute() else p.resolve()
        if resolved.is_relative_to(self.base_dir):
            return resolved
        if self._perm and self._perm.is_read_allowed(str(resolved), self._skill_name):
            return resolved
        raise PermissionError(f"read not permitted: {path_str!r} (outside project)")

    def _resolve_write(self, path_str: str) -> Path:
        p = Path(path_str).expanduser()
        if p.is_absolute():
            resolved = p.resolve()
            if self._perm and self._perm.is_write_allowed(str(resolved), self._skill_name):
                return resolved
            raise PermissionError(
                f"write not permitted: {path_str!r} (absolute paths are read-only)"
            )
        resolved = (self.base_dir / p).resolve()
        if resolved.is_relative_to(self.base_dir):
            return resolved
        if self._perm and self._perm.is_write_allowed(str(resolved), self._skill_name):
            return resolved
        raise PermissionError(f"path escapes project: {path_str!r}")

    def read_file(self, path_str: str) -> tuple[str, bool]:
        """Read a file. Returns (content, found). Raises PermissionError if denied."""
        path = self._resolve_read(path_str)
        if path.exists():
            return path.read_text(encoding="utf-8"), True
        return "", False

    def write_file(self, path_str: str, content: str) -> None:
        """Write a file into the project. Raises PermissionError if denied."""
        path = self._resolve_write(path_str)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self._events.emit("workspace_updated", path=str(path))

    def delete_file(self, path_str: str) -> bool:
        """Delete a file from the project. Returns True if deleted, False if not found."""
        path = self._resolve_write(path_str)
        if path.exists() and path.is_file():
            path.unlink()
            self._events.emit("workspace_updated", path=str(path))
            return True
        return False

    def glob_files(self, pattern: str, max_results: int = 50) -> list[str]:
        """
        Expand a glob pattern. Relative patterns resolve under base_dir (CWD).
        Returns project-relative path strings.
        """
        p = Path(pattern)
        if p.is_absolute():
            resolved_root = p
            if not any(
                str(resolved_root).startswith(str(r))
                for r in [self.base_dir, self.state_dir]
            ):
                # Outside project root — consult PermissionResolver.
                # stdlib skills and other legitimate read targets may live
                # outside the project directory; the permission system is the
                # canonical gate for those paths.
                # For glob patterns that contain wildcards, extract the
                # longest concrete prefix (the root before any wildcard
                # component) and check read permission against that base.
                pattern_str = str(resolved_root)
                # Find the first component that contains a glob special char
                parts = resolved_root.parts
                concrete_parts = []
                for part in parts:
                    if any(c in part for c in ("*", "?", "[")):
                        break
                    concrete_parts.append(part)
                base_for_check = str(Path(*concrete_parts)) if concrete_parts else pattern_str
                if not (
                    self._perm is not None
                    and self._perm.is_read_allowed(base_for_check, self._skill_name)
                ):
                    raise PermissionError(
                        f"glob not permitted: {pattern!r} (outside project, no read permission)"
                    )
            raw = sorted(_glob.glob(pattern, recursive=True))[:max_results]
            return [m for m in raw if Path(m).is_file()]

        ws_matches = sorted(self.base_dir.glob(pattern))
        result = []
        for m in ws_matches[:max_results]:
            if m.is_file():
                try:
                    result.append(str(m.relative_to(self.base_dir)))
                except ValueError:
                    pass
        return result

    def store_artifact(
        self,
        phase: str,
        artifact: dict,
        *,
        skill_name: str = "_unknown",
        visit: int = 1,
    ) -> str:
        """
        Persist artifact to state_dir/artifacts/{skill_name}/{phase}/v{visit}_{type}.json.
        Returns the state_dir-relative path.
        """
        artifact_type = artifact.get("type", "unknown")

        def _safe(s: str) -> str:
            return s.replace("/", "_").replace(" ", "_")

        rel = (
            f"artifacts/{_safe(skill_name)}/{_safe(phase)}"
            f"/v{visit:02d}_{_safe(artifact_type)}.json"
        )
        abs_path = self.state_dir / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Return path relative to base_dir so the LLM can read it via file ops
        base_rel = str(abs_path.relative_to(self.base_dir))
        self.artifacts.append({"phase": phase, "artifact": artifact, "path": base_rel})
        inner = artifact.get("data", artifact)
        keys = list(inner.keys()) if isinstance(inner, dict) else []
        self._events.emit(
            "artifact_created",
            phase=phase,
            artifact_type=artifact_type,
            keys=keys,
            path=base_rel,
        )
        return base_rel
