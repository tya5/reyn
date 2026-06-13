"""Workspace file-content versioning via a content-addressed shadow-git store.

ADR-0038 Stage 1d (D9) + #1544 (container mode). The runtime side of a generation
lives in ``SnapshotGenerationStore`` (per-agent ``AgentSnapshot`` by WAL seq); the
*workspace files* are the other substrate and live here. A generation ties the
two at a boundary seq:

    generation(N) = {per-agent AgentSnapshot @ N}  ⊗  {shadow-git commit @ N}

This store is the workspace half: ONE content-addressed shadow-git repo (the
workspace is a single SSoT — ADR D2) keyed by the GLOBAL WAL seq via tags
``reyn-gen-<seq>``.

**Git execution is delegated to a runner** (#1544) so the same store works on the
host (a ``subprocess`` runner) and inside a container (a ``backend.run`` /
``docker exec`` runner — the work-tree is container-side, so host git cannot reach
it). The runner owns the ``--git-dir`` / ``--work-tree`` path context for its
environment; the store builds bare git args. Git methods are **async** because the
container runner is async (every call-site is already in an async context — see
#1544 enumeration). **git-absence degrades at exec time** (the runner raises;
methods log-once + no-op) — checking ``shutil.which`` would test the *host* PATH,
which is meaningless for a container.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_TAG_PREFIX = "reyn-gen-"
_TAG_RE = re.compile(rf"^{re.escape(_TAG_PREFIX)}(\d+)$")
# Marker committer identity — shadow commits never touch the user's git identity.
_SHADOW_NAME = "reyn-shadow"
_SHADOW_EMAIL = "reyn@shadow.local"


class GitUnavailable(Exception):
    """The git binary is absent in the runner's environment (degrade signal)."""


class _HostGitRunner:
    """Runs git on the host via ``subprocess`` (the default, behavior-preserving).

    Owns the host ``--git-dir`` / ``--work-tree`` path context. Raises
    ``GitUnavailable`` when the git binary is missing so the store degrades.
    """

    def __init__(self, git_dir: Path, work_tree: Path) -> None:
        self._git_dir = Path(git_dir)
        self._work_tree = Path(work_tree)

    async def run(self, args: list[str], *, check: bool = True) -> tuple[int, str]:
        cmd = [
            "git",
            "--git-dir", str(self._git_dir),
            "--work-tree", str(self._work_tree),
            "-c", f"user.name={_SHADOW_NAME}",
            "-c", f"user.email={_SHADOW_EMAIL}",
            *args,
        ]
        try:
            # Inline (blocking) subprocess — same as the pre-#1544 host behavior
            # (host git already ran sync inside the async turn loop, briefly).
            result = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError as e:  # git binary absent on the host
            raise GitUnavailable("git binary not found on host PATH") from e
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr,
            )
        return result.returncode, result.stdout


class _ContainerGitRunner:
    """Runs git INSIDE a container via ``backend.run`` (docker exec) — #1544.

    The work-tree is container-side (the agent edits files there), so host git
    cannot reach it; git must run in-container. Owns the CONTAINER
    ``--git-dir`` / ``--work-tree`` path context (e.g. ``/workspace/.reyn/...`` /
    ``/workspace``). git-absence in the container surfaces as rc 127
    (``command not found``) → ``GitUnavailable`` so the store degrades — the
    correct check (vs the host PATH, which is meaningless for a container).
    """

    def __init__(self, backend: "object", *, git_dir: str, work_tree: str) -> None:
        self._backend = backend
        self._git_dir = git_dir
        self._work_tree = work_tree

    async def run(self, args: list[str], *, check: bool = True) -> tuple[int, str]:
        from reyn.sandbox.policy import SandboxPolicy

        argv = [
            "git",
            "--git-dir", self._git_dir,
            "--work-tree", self._work_tree,
            "-c", f"user.name={_SHADOW_NAME}",
            "-c", f"user.email={_SHADOW_EMAIL}",
            *args,
        ]
        # backend.run honors only policy.timeout_seconds (the container is the
        # isolation boundary); defaults suffice for this trusted infra command.
        res = await self._backend.run(argv, SandboxPolicy())
        out = res.stdout.decode() if isinstance(res.stdout, bytes) else (res.stdout or "")
        if res.returncode == 127:  # shell: git not found in the container
            raise GitUnavailable("git binary not found in container")
        if check and res.returncode != 0:
            raise subprocess.CalledProcessError(res.returncode, argv, out)
        return res.returncode, out


class WorkspaceVersionStore:
    """Content-addressed shadow-git store for workspace files (ADR-0038 1d, #1544).

    Parameters
    ----------
    workspace_root:
        The work-tree captured/restored (host path; for the host runner also the
        git ``--work-tree``).
    git_dir:
        Where the shadow repo lives (e.g. ``.reyn/workspace-shadow.git``). The
        store does its small FS surface (init dir + ``info/exclude``) here.
    exclude:
        Paths (relative to the work-tree) the shadow never tracks. Default
        ``[".reyn/"]`` so OS state is never tracked nor wiped on restore.
    git_runner:
        Executes git in the target environment (default: a host ``subprocess``
        runner). A container runner (#1544) runs ``backend.run`` with the
        container path context.
    """

    def __init__(
        self,
        workspace_root: Path,
        git_dir: Path,
        *,
        exclude: list[str] | None = None,
        git_runner: "object | None" = None,
    ) -> None:
        self._work_tree = Path(workspace_root)
        self._git_dir = Path(git_dir)
        self._exclude = list(exclude) if exclude is not None else [".reyn/"]
        self._runner = git_runner or _HostGitRunner(self._git_dir, self._work_tree)
        self._warned_unavailable = False

    # ── availability ──────────────────────────────────────────────────────

    @staticmethod
    def host_git_available() -> bool:
        """Host-only fast-path: is a ``git`` binary on the host PATH?

        Convenience for host callers; NOT the correctness gate (container git
        presence is checked at exec time — see module docstring). Container mode
        degrades when the runner raises ``GitUnavailable``.
        """
        return shutil.which("git") is not None

    # ── capture / restore ─────────────────────────────────────────────────

    async def capture(self, seq: int) -> str | None:
        """Capture the current workspace as the generation tagged ``seq``.

        ``add -A`` + commit + tag ``reyn-gen-<seq>``. Idempotent per seq: if the
        tag already exists, returns the existing sha without re-committing.
        Returns the commit sha, or ``None`` when git is unavailable (degrade).
        """
        try:
            await self._ensure_repo()
            existing = await self._tag_sha(self._tag(seq))
            if existing is not None:
                return existing
            await self._git(["add", "-A"])
            # --allow-empty: a no-change boundary still gets a generation so every
            # seq is restorable (matches the runtime snapshot at that seq).
            await self._git(
                ["commit", "--allow-empty", "-q", "-m", f"reyn generation @ seq {seq}"],
            )
            await self._git(["tag", self._tag(seq)])
            return await self._rev_parse("HEAD")
        except GitUnavailable:
            return self._degrade("capture", seq)

    async def capture_tree(self) -> str | None:
        """Capture the current workspace as a bare **tree** object (#1560 act-turn).

        ``add -A`` + ``write-tree`` — a content-addressed tree snapshot with **no
        commit and no tag**, so it is much cheaper than :meth:`capture` (the
        per-boundary generation) yet fully coherent (multi-file, deletions, renames
        are all captured; git dedups unchanged subtrees/blobs). Used by the opt-in
        per-step act-turn content log: a caller records ``(op_seq, tree_sha)`` and
        later restores via ``read-tree`` of the tree. Returns the tree sha, or
        ``None`` when git is unavailable (degrade) — best-effort, never raises.
        """
        try:
            await self._ensure_repo()
            await self._git(["add", "-A"])
            out = await self._git(["write-tree"])
            return out.strip() or None
        except GitUnavailable:
            return self._degrade("capture_tree", None)

    async def restore_at_or_below(self, seq: int) -> str | None:
        """Restore to the nearest generation with **raw** tag-seq <= ``seq``.

        Convenience for the no-rewind / single-branch case. **When rewind records
        exist, callers MUST use** :meth:`restore_to_seq` with an is_active-resolved
        seq — a raw nearest-below can land on an abandoned-branch generation.
        """
        base = await self._nearest_at_or_below(seq)
        return await self.restore_to_seq(base) if base is not None else None

    async def restore_to_seq(self, seq: int) -> str | None:
        """Restore the workspace to the EXACT generation tagged ``seq``.

        ``reset --hard`` + ``clean -fd`` (honoring the excludes) so files added
        after the generation are removed while excluded OS state (``.reyn/``)
        survives. The caller supplies the precise (is_active-resolved) seq. Returns
        the restored sha, or ``None`` when git is unavailable / no such tag.
        """
        try:
            tag = self._tag(seq)
            if await self._tag_sha(tag) is None:
                return None
            await self._git(["reset", "--hard", "-q", tag])
            clean_args = ["clean", "-fdq"]
            for pat in self._exclude:
                clean_args += ["-e", pat]
            await self._git(clean_args)
            return await self._tag_sha(tag)
        except GitUnavailable:
            return self._degrade("restore", seq)

    # ── queries ──────────────────────────────────────────────────────────

    async def seqs(self) -> list[int]:
        """Sorted captured generation seqs (from ``reyn-gen-*`` tags); [] if degraded."""
        try:
            out = await self._git(["tag", "--list", f"{_TAG_PREFIX}*"], check=False)
        except GitUnavailable:
            return self._degrade("seqs", None) or []
        found = []
        for line in (out or "").splitlines():
            m = _TAG_RE.match(line.strip())
            if m:
                found.append(int(m.group(1)))
        return sorted(found)

    async def prune_below(self, min_keep_seq: int) -> int:
        """Delete generations with seq < ``min_keep_seq`` (Stage 1e retention GC)."""
        try:
            removed = 0
            for s in await self.seqs():
                if s < min_keep_seq:
                    await self._git(["tag", "-d", self._tag(s)], check=False)
                    removed += 1
            return removed
        except GitUnavailable:
            return self._degrade("prune", min_keep_seq) or 0

    # ── internals ──────────────────────────────────────────────────────────

    def _degrade(self, op: str, seq: "int | None"):
        if not self._warned_unavailable:
            logger.warning(
                "git unavailable in workspace-version runner — workspace "
                "versioning disabled (op=%s seq=%s); rewind = runtime-only", op, seq,
            )
            self._warned_unavailable = True
        return None

    def _tag(self, seq: int) -> str:
        return f"{_TAG_PREFIX}{int(seq)}"

    async def _nearest_at_or_below(self, seq: int) -> int | None:
        candidates = [s for s in await self.seqs() if s <= seq]
        return max(candidates) if candidates else None

    async def _ensure_repo(self) -> None:
        """Initialise the shadow repo (idempotent) + write the exclude file.

        The small FS surface (dir + ``info/exclude``) runs on the host git-dir
        path — in container mount-mode that path is the bind-mount source, so the
        write is visible in-container; ``git init`` runs through the runner.
        """
        if not (self._git_dir / "HEAD").exists():
            self._git_dir.mkdir(parents=True, exist_ok=True)
            await self._git(["init", "-q"])
        info = self._git_dir / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "exclude").write_text(
            "".join(f"{pat}\n" for pat in self._exclude), encoding="utf-8",
        )

    async def _git(self, args: list[str], *, check: bool = True) -> str:
        _, out = await self._runner.run(args, check=check)
        return out

    async def _rev_parse(self, ref: str) -> str | None:
        try:
            out = await self._git(["rev-parse", ref])
        except subprocess.CalledProcessError:
            return None
        return out.strip() or None

    async def _tag_sha(self, tag: str) -> str | None:
        rc, out = await self._runner.run(
            ["rev-parse", "--verify", "-q", f"{tag}^{{commit}}"], check=False,
        )
        return out.strip() if rc == 0 and out.strip() else None
