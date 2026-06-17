"""EnvironmentBackend Protocol — repo-filesystem mechanism abstraction (FP-0008 #1115 Stage 1).

The Protocol decouples the :class:`~reyn.data.workspace.workspace.Workspace` from the
concrete filesystem the repo working tree lives on. The **host** backend
(:class:`~reyn.environment.host_backend.HostBackend`) is an identity over the
local Python filesystem — exactly the behavior Workspace had inline before this
seam existed. A later stage (#1115 Stage 2) adds a container backend so the
repo FS can live inside a container while the OS + permission layer stay on the
host (industry pattern: OpenHands Runtime / Hermes docker-exec).

Contract / division of responsibility (behavior-preserving):
  - Workspace owns the **permission gate** (``_resolve_read`` / ``_resolve_write``
    against ``base_dir`` + the PermissionResolver), **relative-path resolution**,
    and **event emission** (``workspace_updated``).
  - The backend receives ABSOLUTE, already-permission-resolved paths and
    performs only the IO. This keeps the permission boundary host-side and
    enforced uniformly regardless of where the repo FS lives.

``grep`` is a **primitive** (an environment-internal scan), not a host-side
composition of glob + N reads: against a container backend a host-composed
scan would need N round-trips, so the scan must run *inside* the environment
(the host backend runs Python ``re``; a container backend runs the scan in the
container, preserving Python ``re`` semantics). ``edit``, by contrast, is a
pure host-side transform and stays a **composition**
(``Workspace.edit`` = backend read → ``str.replace`` → backend write) rather
than a backend primitive.

``exec`` convergence with the FP-0017 ``SandboxBackend`` is intentionally NOT
part of this Protocol yet — ``exec`` already has a backend abstraction
(``reyn.security.sandbox``); folding it in is a separate step (#1115 Stage 1b) to keep
this repo-FS seam behavior-preserving and reviewable in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Pattern, Protocol, runtime_checkable


@dataclass
class GrepResult:
    """Result of an environment-internal grep scan.

    Populated according to ``output_mode``:
      - ``"files_with_matches"`` → ``files`` (absolute Paths of matching files)
      - ``"count"``              → ``count`` (total match count)
      - ``"content"``            → ``matches`` (list of hit dicts, ``path`` as a
        Path; the caller relativizes for presentation)
    """

    output_mode: str
    files: list[Path] = field(default_factory=list)
    count: int = 0
    matches: list[dict] = field(default_factory=list)


@runtime_checkable
class EnvironmentBackend(Protocol):
    """Repo-filesystem backend protocol.

    Implementations declare a ``name`` attribute (= ``"host"`` now; ``"container"``
    at Stage 2) and operate on ABSOLUTE paths the Workspace has already resolved
    and permission-checked. See the module docstring for the
    responsibility split.
    """

    name: str

    def read_bytes(self, path: Path) -> bytes | None:
        """Return the file's bytes, or ``None`` if the path does not exist."""
        ...

    def write_bytes(self, path: Path, data: bytes) -> None:
        """Write ``data`` to ``path``, creating parent directories as needed."""
        ...

    def delete(self, path: Path) -> bool:
        """Delete a regular file. Return ``True`` if removed, ``False`` if the
        path is absent or is not a regular file."""
        ...

    def mkdir(self, path: Path, *, parents: bool = True) -> bool:
        """Create a directory. Return ``True`` if newly created, ``False`` if it
        already existed as a directory. Raise ``FileExistsError`` if a
        non-directory already sits at ``path``."""
        ...

    def move(self, src: Path, dst: Path) -> bool:
        """Move ``src`` to ``dst`` (creating ``dst`` parents). Return ``True`` on
        success, ``False`` if ``src`` does not exist."""
        ...

    def stat(self, path: Path) -> dict | None:
        """Return a metadata dict (``size`` / ``mtime`` / ``ctime`` / ``is_dir`` /
        ``is_file`` / ``mode``), or ``None`` if ``path`` does not exist."""
        ...

    def glob(self, pattern: str, *, root: Path | None = None) -> list[Path]:
        """Expand a glob pattern, returning matching FILES only.

        When ``root`` is ``None``, ``pattern`` is an absolute pattern matched
        recursively. Otherwise ``pattern`` is matched relative to ``root``.

        Directories are excluded: each backend filters to files in its own
        environment, symmetric with :meth:`grep` (#1375 D10). The contract was
        narrowed from "all matches; caller filters" because a host-side file
        filter cannot stat a container backend's paths — so the only sound place
        for the filter is the environment that produced the matches. The sole
        consumer (``Workspace.glob_files``) wants files only. If a directory
        glob is ever needed, add a separate path (e.g. ``glob_dirs``) rather than
        widening this one back to a mixed result.
        """
        ...

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
        """Scan files under ``root`` for ``regex`` and return a :class:`GrepResult`.

        ``root`` is an absolute, permission-resolved path. When ``root`` is a
        file, only that file is scanned; otherwise files matching ``glob``
        (default ``**/*``) under it, optionally filtered to ``file_type``
        (extension), are scanned. This is the environment-internal scan
        primitive (see module docstring).
        """
        ...
