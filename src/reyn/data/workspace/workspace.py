from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from reyn.core.events.events import EventLog
from reyn.environment.host_backend import HostBackend

if TYPE_CHECKING:
    from reyn.environment.backend import EnvironmentBackend, GrepResult
    from reyn.security.permissions.permissions import PermissionResolver


@dataclass
class GlobFilesResult:
    """Result of :meth:`Workspace.glob_files_with_total` (#2998).

    ``matches`` is the ``max_results``-capped list (identical to what
    :meth:`Workspace.glob_files` returns for the same args); ``total`` is the
    PRE-CAP match count, so a caller can tell ``total > len(matches)`` — the
    cap actually discarded matches — from ``total == len(matches)`` — nothing
    was cut."""

    matches: list[str]
    total: int


class Workspace:
    """
    Workspace where the agent operates.

    base_dir  : CWD — where relative file paths resolve (read + write).
    state_dir : .reyn/ — where event logs and invoke sub-dirs live.

    Read  policy : any path under base_dir (CWD), plus paths the PermissionResolver
                   has approved for this agent (declared via `permissions.file.read`).
    Write policy : any path under base_dir (CWD), plus paths the PermissionResolver
                   has approved for this agent (declared via `permissions.file.write`).
    """

    def __init__(
        self,
        events: EventLog,
        permission_resolver: "PermissionResolver | None" = None,
        actor: str = "",
        base_dir: "Path | None" = None,
        state_dir: "Path | None" = None,
        environment_backend: "EnvironmentBackend | None" = None,
    ) -> None:
        # FP-0008 #1115 Stage 1: the repo working tree is accessed through a
        # pluggable EnvironmentBackend. Default = HostBackend (identity over the
        # local filesystem = legacy behavior). Stage 2 supplies a container
        # backend so the repo FS can live in a container while this OS layer +
        # the permission gate stay host-side. The permission gate, relative-path
        # resolution, and event emission stay here; the backend does only IO on
        # the absolute paths this class resolves.
        self._backend: "EnvironmentBackend" = environment_backend or HostBackend()
        # #1390 L3: a host backend for state_dir reads. state_dir storage is
        # host-side (writes under state_dir go directly host-side, bypassing the
        # repo backend), so reads of state_dir paths must mirror that split —
        # they stay host-side, not routed through the repo/container backend
        # (under the docker backend a host state_dir path does not exist
        # in-container). A fresh HostBackend reads reyn's own process FS = where
        # state_dir content was written. When self._backend is already a
        # HostBackend (non-docker), this is the same environment, so routing is
        # behaviour-preserving there.
        self._state_backend: "EnvironmentBackend" = HostBackend()
        self.base_dir = base_dir.resolve() if base_dir is not None else Path.cwd()
        # FP-0008 #1115 Stage 0: state_dir is host-side and decoupled from
        # base_dir. Default = base_dir/.reyn (backward-compat). A caller that
        # routes the repo FS through a backend (e.g. a container base_dir)
        # passes an explicit host-side state_dir, so state survives
        # independently of the repo working tree (container-death recoverable).
        self.state_dir = (
            state_dir.resolve()
            if state_dir is not None
            else (self.base_dir / ".reyn").resolve()
        )
        self._events = events
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._perm = permission_resolver
        self._actor = actor

    @property
    def backend(self) -> "EnvironmentBackend":
        """The EnvironmentBackend this workspace's IO runs on (#1200): the
        injected instance, or the default HostBackend. Read-only — for wiring
        verification (e.g. confirming chat/plan/phase share one agent backend)."""
        return self._backend

    def _resolve_read(self, path_str: str) -> Path:
        p = Path(path_str).expanduser()
        resolved = (self.base_dir / p).resolve() if not p.is_absolute() else p.resolve()
        if resolved.is_relative_to(self.base_dir):
            return resolved
        if self._perm and self._perm.is_read_allowed(str(resolved), self._actor):
            return resolved
        raise PermissionError(f"read not permitted: {path_str!r} (outside project)")

    def _read_backend_for(self, resolved_path: Path) -> "EnvironmentBackend":
        """Backend for reading ``resolved_path`` — the single state_dir routing
        seam (#1390 L3).

        A read whose RESOLVED path is under ``state_dir`` (e.g. an OS-offloaded
        artifact the agent is told to ``file.read``, ``llm.py``) stays host-side;
        every other read goes to the repo backend. ``state_dir`` is ``.resolve()``
        d at construction (``/tmp`` ↔ ``/private/tmp`` normalised), so callers
        MUST pass a resolved path (``_resolve_read`` output) — a raw path would
        mis-compare and wrongly route a state_dir read to the container backend
        (the bug this fixes). Every backend-read site routes through here so no
        site can silently miss the split (completeness by construction).
        """
        if resolved_path.is_relative_to(self.state_dir):
            return self._state_backend
        return self._backend

    def _resolve_write(self, path_str: str) -> Path:
        p = Path(path_str).expanduser()
        if p.is_absolute():
            resolved = p.resolve()
            if self._perm and self._perm.is_write_allowed(str(resolved), self._actor):
                return resolved
            raise PermissionError(
                f"write not permitted: {path_str!r} (absolute paths are read-only)"
            )
        resolved = (self.base_dir / p).resolve()
        if resolved.is_relative_to(self.base_dir):
            return resolved
        if self._perm and self._perm.is_write_allowed(str(resolved), self._actor):
            return resolved
        raise PermissionError(f"path escapes project: {path_str!r}")

    def read_file(self, path_str: str) -> tuple[str, bool]:
        """Read a file. Returns (content, found). Raises PermissionError if denied."""
        path = self._resolve_read(path_str)
        data = self._read_backend_for(path).read_bytes(path)
        if data is None:
            return "", False
        return data.decode("utf-8"), True

    def read_file_bytes(self, path_str: str) -> tuple[bytes, bool]:
        """Read a file as raw bytes (issue #365).

        Mirrors ``read_file`` but skips text decoding — used by the file
        handler's binary path (image/* extensions). Returns
        ``(content_bytes, found)``. Raises PermissionError if the read is
        denied by the workspace policy.
        """
        path = self._resolve_read(path_str)
        data = self._read_backend_for(path).read_bytes(path)
        if data is None:
            return b"", False
        return data, True

    def write_file(self, path_str: str, content: str) -> None:
        """Write a file into the project. Raises PermissionError if denied."""
        path = self._resolve_write(path_str)
        self._backend.write_bytes(path, content.encode("utf-8"))
        self._events.emit("workspace_updated", path=str(path))

    def write_file_bytes(self, path_str: str, data: bytes, *, emit: bool = True) -> str:
        """Write raw bytes into the project (#1452 — the write-side mirror of
        ``read_file_bytes``). Used by file__edit / write to persist content
        already encoded in the file's detected codec (preserving a non-UTF-8
        encoding + BOM on in-place edits). Same write-zone gating as
        ``write_file``. Raises PermissionError if denied.

        Returns the resolved absolute path string that was written.

        ``emit=False`` (#2782) skips the ``workspace_updated`` emit — for a
        caller running this off the event loop (an ``asyncio.to_thread``
        worker), which MUST emit itself afterward, back on the loop thread.
        A worker-thread ``ctx.events.emit`` reaches ``EventStore.write``,
        which calls ``asyncio.get_running_loop()`` — off-loop, that RAISES,
        falling to a non-serialized sync-fallback write path that mutates
        ``EventStore``'s rotation state without the loop-thread-only
        serialization protecting it, racing the DurabilityWorker's own
        writes to the same file (the #2780/#2784 off-loop thread-safety
        contract this must not violate)."""
        path = self._resolve_write(path_str)
        self._backend.write_bytes(path, data)
        if emit:
            self._events.emit("workspace_updated", path=str(path))
        return str(path)

    def delete_file(self, path_str: str) -> bool:
        """Delete a file from the project. Returns True if deleted, False if not found."""
        path = self._resolve_write(path_str)
        deleted = self._backend.delete(path)
        if deleted:
            self._events.emit("workspace_updated", path=str(path))
        return deleted

    def make_directory(self, path_str: str, *, parents: bool = True) -> bool:
        """Create a directory under the project (issue #356).

        Idempotent: returns True if newly created, False if the directory
        already existed. Raises FileExistsError if a non-directory
        (= a regular file) sits at the path. Raises PermissionError via
        ``_resolve_write`` if the path is outside the project and not
        explicitly approved.
        """
        path = self._resolve_write(path_str)
        try:
            created = self._backend.mkdir(path, parents=parents)
        except FileExistsError:
            # Preserve the legacy message which embeds the caller's path_str.
            raise FileExistsError(
                f"path exists but is not a directory: {path_str!r}"
            ) from None
        if created:
            self._events.emit("workspace_updated", path=str(path))
        return created

    def move_path(self, src_str: str, dst_str: str) -> bool:
        """Move / rename a file or directory (issue #356).

        Requires write permission on BOTH source (= effectively a delete)
        and destination (= effectively a write). Returns True on success,
        False if the source does not exist.
        """
        src = self._resolve_write(src_str)
        dst = self._resolve_write(dst_str)
        moved = self._backend.move(src, dst)
        if moved:
            self._events.emit("workspace_updated", path=str(dst))
        return moved

    def stat_path(self, path_str: str) -> dict | None:
        """Filesystem metadata for a file / directory (issue #356).

        Returns ``None`` if the path does not exist. Otherwise returns a
        dict with ``size`` (bytes), ``mtime`` / ``ctime`` (epoch seconds,
        float), ``is_dir``, ``is_file``, and ``mode`` (= octal permissions
        string, e.g. ``"0o644"``). Gated by ``_resolve_read``.
        """
        path = self._resolve_read(path_str)
        return self._read_backend_for(path).stat(path)

    def glob_files(
        self, pattern: str, max_results: int = 50, *, absolute: bool = False
    ) -> list[str]:
        """
        Expand a glob pattern. Relative patterns resolve under base_dir (CWD).
        Returns project-relative path strings by default.

        ``absolute=True`` (#3102) makes the RELATIVE-pattern branch return
        absolute path strings instead of relativizing them against
        ``base_dir`` — opt-in, so every existing caller that relies on the
        project-relative default (the vast majority: display paths, "not
        found" suggestions, etc.) is unaffected. A consumer that hands a
        glob match straight into a ``file://`` URI (e.g. rag_ingest.yaml)
        needs an absolute path regardless of whether the caller's own
        pattern happened to be relative or absolute — this flag lets it ask
        for that directly instead of leaving the caller to reconstruct an
        absolute path itself (R1 pipelines have no abspath/cwd primitive to
        do that with). The absolute-pattern branch already returns absolute
        paths unconditionally, so this flag is a no-op there.

        Callers that must know whether this cap actually discarded matches
        (#2998 — a silent 50-cap otherwise reads as "this is everything")
        use :meth:`glob_files_with_total` instead, which shares this exact
        walk + permission logic via :meth:`_glob_files_uncapped` and returns
        the pre-cap total alongside the capped list.
        """
        return self._glob_files_uncapped(pattern, absolute=absolute)[:max_results]

    def glob_files_with_total(
        self, pattern: str, max_results: int = 50, *, absolute: bool = False
    ) -> "GlobFilesResult":
        """Like :meth:`glob_files`, but also reports the PRE-CAP total match count
        (#2998). Both branches of the shared walk already build the full match list
        before slicing to ``max_results``, so ``len(...)`` before the slice is free —
        no extra glob pass. Callers that must signal a silent truncation to their
        consumer (the ``glob`` / ``list_directory`` file ops) use this instead of
        :meth:`glob_files`.
        """
        all_matches = self._glob_files_uncapped(pattern, absolute=absolute)
        return GlobFilesResult(matches=all_matches[:max_results], total=len(all_matches))

    def _glob_files_uncapped(self, pattern: str, *, absolute: bool = False) -> list[str]:
        """Shared walk + permission-gate logic behind :meth:`glob_files` /
        :meth:`glob_files_with_total` (#2998 extraction) — returns the FULL,
        un-capped match list; callers slice to their own ``max_results``."""
        p = Path(pattern)
        if p.is_absolute():
            resolved_root = p
            if not any(
                str(resolved_root).startswith(str(r))
                for r in [self.base_dir, self.state_dir]
            ):
                # Outside project root — consult PermissionResolver.
                # Safe-mode python steps and other legitimate read targets
                # may live outside the project directory; the permission
                # system is the canonical gate for those paths.
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
                    and self._perm.is_read_allowed(base_for_check, self._actor)
                ):
                    raise PermissionError(
                        f"glob not permitted: {pattern!r} (outside project, no read permission)"
                    )
            # The backend returns files only — directories are excluded in the
            # backend's own environment (#1375 D10), so capping at max_results
            # here cannot silently truncate the file list to ~zero behind leading
            # directories. Re-applying a host-side ``is_file()`` filter would be
            # the D10 bug: it stats a container backend's paths against the host
            # filesystem (where they do not exist) and drops everything.
            # #1390 L3: route by the resolved glob root — a state_dir-rooted glob
            # stays host-side (same split as read_file), repo globs hit the repo
            # backend. (glob_files permits state_dir roots above.)
            files = sorted(str(m) for m in self._read_backend_for(resolved_root).glob(pattern))
            return files

        # Relative-path branch: backend is already files-only (#1375 D10);
        # relativize. base_dir is never under state_dir, so this routes to
        # the repo backend (the #1390 L3 seam, uniformly applied).
        ws_matches = sorted(
            self._read_backend_for(self.base_dir).glob(pattern, root=self.base_dir)
        )
        if absolute:
            # backend.glob(root=self.base_dir) already returns paths rooted
            # at base_dir (Path.glob(root / pattern) is absolute when root
            # is) — no relativize/re-resolve needed, just pass them through.
            return [str(m) for m in ws_matches]
        result = []
        for m in ws_matches:
            try:
                result.append(str(m.relative_to(self.base_dir)))
            except ValueError:
                pass
        return result

    def grep(
        self,
        path_str: str,
        regex: "re.Pattern[str]",
        *,
        glob: str | None = None,
        file_type: str | None = None,
        output_mode: str = "content",
        head_limit: int | None = None,
        context_before: int = 0,
        context_after: int = 0,
    ) -> "GrepResult":
        """Permission-resolve the search root and run the backend's scan.

        FP-0008 #1115 Stage 1: ``grep`` is an environment-internal scan
        primitive — the Workspace gates the root via ``_resolve_read`` (raising
        PermissionError when denied) and delegates the glob+read+regex scan to
        the backend, which returns absolute Paths. The caller (file op handler)
        relativizes for presentation. See [[environment.backend]] module docs.
        """
        root = self._resolve_read(path_str)
        # #1390 L3: a state_dir-rooted grep stays host-side (same split as
        # read_file); repo greps hit the repo backend.
        return self._read_backend_for(root).grep(
            root,
            regex,
            glob=glob,
            file_type=file_type,
            output_mode=output_mode,
            head_limit=head_limit,
            context_before=context_before,
            context_after=context_after,
        )

