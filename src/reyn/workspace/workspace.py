from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from reyn.environment.host_backend import HostBackend
from reyn.events.events import EventLog

if TYPE_CHECKING:
    from reyn.environment.backend import EnvironmentBackend, GrepResult
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
        # host-side (store_artifact writes directly host-side, bypassing the repo
        # backend), so reads of state_dir paths must mirror that split — they stay
        # host-side, not routed through the repo/container backend (under the
        # docker backend a host state_dir path does not exist in-container). A
        # fresh HostBackend reads reyn's own process FS = where store_artifact
        # wrote. When self._backend is already a HostBackend (non-docker), this is
        # the same environment, so routing is behaviour-preserving there.
        self._state_backend: "EnvironmentBackend" = HostBackend()
        self.base_dir = base_dir.resolve() if base_dir is not None else Path.cwd()
        # FP-0008 #1115 Stage 0: state_dir is host-side and decoupled from
        # base_dir. Default = base_dir/.reyn (backward-compat). A caller that
        # routes the repo FS through a backend (e.g. a container base_dir)
        # passes an explicit host-side state_dir, so artifacts + events survive
        # independently of the repo working tree (container-death recoverable).
        self.state_dir = (
            state_dir.resolve()
            if state_dir is not None
            else (self.base_dir / ".reyn").resolve()
        )
        self._events = events
        self.artifacts: list[dict] = []
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "artifacts").mkdir(exist_ok=True)
        self._perm = permission_resolver
        self._skill_name = skill_name

    @property
    def backend(self) -> "EnvironmentBackend":
        """The EnvironmentBackend this workspace's IO runs on (#1200): the
        injected instance, or the default HostBackend. Read-only — for wiring
        verification (e.g. confirming chat/plan/phase share one agent backend)."""
        return self._backend

    def resolve_artifact_handle(self, handle: str) -> Path:
        """Resolve a state_dir-relative artifact handle to an absolute path.

        FP-0008 #1115 Stage 0: artifact handles returned by ``store_artifact``
        are relative to ``state_dir`` (host-side), not ``base_dir``. The OS uses
        this to serve artifact reads (read-by-ref) without exposing a base_dir
        FS path. Raises :class:`PermissionError` if the handle escapes state_dir.
        """
        resolved = (self.state_dir / handle).resolve()
        if not resolved.is_relative_to(self.state_dir):
            raise PermissionError(
                f"artifact handle {handle!r} escapes state_dir {self.state_dir}"
            )
        return resolved

    def _resolve_read(self, path_str: str) -> Path:
        p = Path(path_str).expanduser()
        resolved = (self.base_dir / p).resolve() if not p.is_absolute() else p.resolve()
        if resolved.is_relative_to(self.base_dir):
            return resolved
        if self._perm and self._perm.is_read_allowed(str(resolved), self._skill_name):
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

    def write_file_bytes(self, path_str: str, data: bytes) -> None:
        """Write raw bytes into the project (#1452 — the write-side mirror of
        ``read_file_bytes``). Used by file__edit / write to persist content
        already encoded in the file's detected codec (preserving a non-UTF-8
        encoding + BOM on in-place edits). Same write-zone gating as
        ``write_file``. Raises PermissionError if denied."""
        path = self._resolve_write(path_str)
        self._backend.write_bytes(path, data)
        self._events.emit("workspace_updated", path=str(path))

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
            return files[:max_results]

        # Relative-path branch: backend is already files-only (#1375 D10);
        # relativize and cap. base_dir is never under state_dir, so this routes to
        # the repo backend (the #1390 L3 seam, uniformly applied).
        ws_matches = sorted(
            self._read_backend_for(self.base_dir).glob(pattern, root=self.base_dir)
        )
        result = []
        for m in ws_matches[:max_results]:
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

        # FP-0008 #1115 Stage 0: store the state_dir-relative handle (NOT a
        # base_dir-relative FS path). state_dir is host-side and may be
        # decoupled from base_dir, so `relative_to(base_dir)` would be invalid.
        # The OS resolves this handle against state_dir when serving reads
        # (see resolve_artifact_handle); consumers no longer file.read it.
        handle = rel  # already state_dir-relative: "artifacts/.../v01_*.json"
        self.artifacts.append({"phase": phase, "artifact": artifact, "path": handle})
        inner = artifact.get("data", artifact)
        keys = list(inner.keys()) if isinstance(inner, dict) else []
        self._events.emit(
            "artifact_created",
            phase=phase,
            artifact_type=artifact_type,
            keys=keys,
            path=handle,
        )
        return handle
