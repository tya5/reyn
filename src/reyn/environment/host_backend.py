"""HostBackend — identity EnvironmentBackend over the local Python filesystem.

FP-0008 #1115 Stage 1. This backend reproduces exactly the filesystem behavior
the :class:`~reyn.workspace.workspace.Workspace` performed inline before the
:class:`~reyn.environment.backend.EnvironmentBackend` seam existed — so wiring
Workspace to a ``HostBackend`` is behavior-preserving. It receives ABSOLUTE,
already-permission-resolved paths (the Workspace owns the permission gate +
relative-path resolution + event emission; see the backend module docstring).
"""
from __future__ import annotations

import glob as _glob
from pathlib import Path
from typing import Any, Pattern

from reyn.environment.backend import GrepResult


class HostBackend:
    """Identity backend: the repo working tree IS the local filesystem."""

    name = "host"

    def read_bytes(self, path: Path) -> bytes | None:
        if path.exists():
            return path.read_bytes()
        return None

    def write_bytes(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def delete(self, path: Path) -> bool:
        if path.exists() and path.is_file():
            path.unlink()
            return True
        return False

    def mkdir(self, path: Path, *, parents: bool = True) -> bool:
        if path.exists():
            if path.is_dir():
                return False
            raise FileExistsError(
                f"path exists but is not a directory: {str(path)!r}"
            )
        path.mkdir(parents=parents, exist_ok=False)
        return True

    def move(self, src: Path, dst: Path) -> bool:
        if not src.exists():
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return True

    def stat(self, path: Path) -> dict | None:
        if not path.exists():
            return None
        st = path.stat()
        return {
            "size": st.st_size,
            "mtime": st.st_mtime,
            "ctime": st.st_ctime,
            "is_dir": path.is_dir(),
            "is_file": path.is_file(),
            "mode": oct(st.st_mode & 0o777),
        }

    def glob(self, pattern: str, *, root: Path | None = None) -> list[Path]:
        if root is None:
            # Absolute pattern (recursive) — matches the legacy abs-path branch.
            return [Path(p) for p in _glob.glob(pattern, recursive=True)]
        # Relative pattern matched under root — matches the legacy rel branch.
        return list(root.glob(pattern))

    def grep(
        self,
        root: Path,
        regex: Pattern[str],
        *,
        glob: str | None = None,
        file_type: str | None = None,
        output_mode: str = "content",
        head_limit: int | None = None,
        context_before: int = 0,
        context_after: int = 0,
    ) -> GrepResult:
        # Gather candidate files (env-internal scan; mirrors _execute_grep).
        if root.is_file():
            candidates = [root]
        else:
            glob_pattern = glob or "**/*"
            candidates = sorted(f for f in root.glob(glob_pattern) if f.is_file())
        if file_type:
            ext = file_type.lstrip(".")
            candidates = [f for f in candidates if f.suffix.lstrip(".") == ext]

        if output_mode == "files_with_matches":
            matched: list[Path] = []
            for f in candidates:
                try:
                    if regex.search(f.read_text(encoding="utf-8", errors="replace")):
                        matched.append(f)
                except OSError:
                    continue
            return GrepResult(output_mode="files_with_matches", files=matched)

        if output_mode == "count":
            total = 0
            for f in candidates:
                try:
                    total += len(regex.findall(f.read_text(encoding="utf-8", errors="replace")))
                except OSError:
                    continue
            return GrepResult(output_mode="count", count=total)

        matches: list[dict] = []
        done = False
        for f in candidates:
            if done:
                break
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines):
                if not regex.search(line):
                    continue
                entry: dict[str, Any] = {"path": f, "line_number": i + 1, "content": line}
                if context_before or context_after:
                    start = max(0, i - context_before)
                    end = min(len(lines), i + context_after + 1)
                    entry["context"] = [
                        {"line_number": j + 1, "content": lines[j], "is_match": j == i}
                        for j in range(start, end)
                    ]
                matches.append(entry)
                if head_limit is not None and len(matches) >= head_limit:
                    done = True
                    break
        return GrepResult(output_mode="content", matches=matches)


__all__ = ["HostBackend"]
