from __future__ import annotations
import glob as _glob
import json
from pathlib import Path
from .events import EventLog


class Workspace:
    """
    Workspace with separate read / write path policies.

    Read policy  : workspace (relative paths) + extra_read_roots (absolute paths allowed).
    Write policy : workspace only (relative paths only; absolute writes are always denied).

    Relative paths are always resolved against base_dir.
    Absolute paths are checked against extra_read_roots for reads; denied for writes.
    """

    def __init__(
        self,
        base_dir: str | Path,
        events: EventLog,
        extra_read_roots: list[str | Path] | None = None,
    ) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._events = events
        self.artifacts: list[dict] = []
        self.extra_read_roots: list[Path] = [
            Path(r).resolve() for r in (extra_read_roots or [])
        ]
        (self.base_dir / "artifacts").mkdir(exist_ok=True)

    def _resolve_read(self, path_str: str) -> Path:
        """
        Resolve a path for reading.

        Relative paths → resolved under base_dir (workspace) first.
        If that path doesn't exist, also try CWD — allowed if under an extra_read_root.
        Absolute paths → allowed only if under an extra_read_root.
        """
        p = Path(path_str)
        if not p.is_absolute():
            workspace_resolved = (self.base_dir / p).resolve()
            if workspace_resolved.is_relative_to(self.base_dir):
                if workspace_resolved.exists():
                    return workspace_resolved
                # Fall through: try resolving relative to CWD against extra_read_roots
                cwd_resolved = (Path.cwd() / p).resolve()
                for root in self.extra_read_roots:
                    if cwd_resolved.is_relative_to(root):
                        return cwd_resolved
                # Not in any allowed root — return workspace path (will be not_found)
                return workspace_resolved
            raise PermissionError(f"path escapes workspace: {path_str!r}")

        resolved = p.resolve()
        for root in self.extra_read_roots:
            if resolved.is_relative_to(root):
                return resolved
        raise PermissionError(
            f"read not permitted: {path_str!r}  "
            f"(not under workspace or any --read-allow root)"
        )

    def _resolve_write(self, path_str: str) -> Path:
        """
        Resolve a path for writing.  Only workspace-relative paths are allowed.
        """
        p = Path(path_str)
        if p.is_absolute():
            raise PermissionError(
                f"write not permitted: {path_str!r}  "
                f"(absolute paths are read-only; writes must be workspace-relative)"
            )
        resolved = (self.base_dir / p).resolve()
        if not resolved.is_relative_to(self.base_dir):
            raise PermissionError(f"path escapes workspace: {path_str!r}")
        return resolved

    def read_file(self, path_str: str) -> tuple[str, bool]:
        """Read a file. Returns (content, found). Raises PermissionError if denied."""
        path = self._resolve_read(path_str)
        if path.exists():
            return path.read_text(encoding="utf-8"), True
        return "", False

    def write_file(self, path_str: str, content: str) -> None:
        """Write a file into the workspace. Raises PermissionError if denied."""
        path = self._resolve_write(path_str)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self._events.emit("workspace_updated", path=str(path))

    def glob_files(self, pattern: str, max_results: int = 50) -> list[str]:
        """
        Expand a glob pattern and return matching file paths (no content).

        Relative patterns are expanded under base_dir; returned paths are
        workspace-relative strings.
        Absolute patterns are expanded as-is; each match is checked against
        read_roots (workspace + extra_read_roots).

        Returns at most max_results paths, sorted.
        Raises PermissionError if an absolute pattern's root is not allowed.
        """
        p = Path(pattern)
        if p.is_absolute():
            read_roots = [self.base_dir] + self.extra_read_roots
            # Verify the non-glob prefix is under an allowed root
            # (best-effort: check pattern up to first glob char)
            non_glob = Path(pattern.split("*")[0].split("?")[0].split("[")[0])
            non_glob_resolved = non_glob.resolve() if non_glob.is_absolute() else non_glob
            if not any(
                str(non_glob_resolved).startswith(str(root))
                for root in read_roots
            ):
                raise PermissionError(
                    f"glob not permitted: {pattern!r}  "
                    f"(pattern root not under workspace or any --read-allow root)"
                )
            raw_matches = sorted(_glob.glob(pattern, recursive=True))[:max_results]
            return [m for m in raw_matches if Path(m).is_file()]
        else:
            # First try workspace-relative glob
            ws_matches = sorted(self.base_dir.glob(pattern))
            result = []
            for m in ws_matches[:max_results]:
                if m.is_file():
                    try:
                        rel = m.relative_to(self.base_dir)
                        result.append(str(rel))
                    except ValueError:
                        pass
            if result:
                return result
            # Fall through: try CWD-relative glob, filter by extra_read_roots
            cwd_matches = sorted(Path.cwd().glob(pattern))
            for m in cwd_matches[:max_results]:
                if not m.is_file():
                    continue
                resolved = m.resolve()
                for root in self.extra_read_roots:
                    if resolved.is_relative_to(root):
                        result.append(str(m))
                        break
            return result

    def store_artifact(
        self,
        phase: str,
        artifact: dict,
        *,
        app_name: str = "_unknown",
        visit: int = 1,
    ) -> str:
        """
        Persist artifact to disk under artifacts/{app_name}/{phase}/v{visit:02d}_{artifact_type}.json.
        Returns the workspace-relative path to the saved file.
        """
        artifact_type = artifact.get("type", "unknown")

        def _safe(s: str) -> str:
            return s.replace("/", "_").replace(" ", "_")

        filename = (
            f"artifacts/{_safe(app_name)}/{_safe(phase)}"
            f"/v{visit:02d}_{_safe(artifact_type)}.json"
        )

        abs_path = self.base_dir / filename
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        self.artifacts.append({"phase": phase, "artifact": artifact, "path": filename})
        inner = artifact.get("data", artifact)
        keys = list(inner.keys()) if isinstance(inner, dict) else []
        self._events.emit(
            "artifact_created",
            phase=phase,
            artifact_type=artifact_type,
            keys=keys,
            path=filename,
        )
        return filename
