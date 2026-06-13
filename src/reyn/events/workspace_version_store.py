"""Workspace file-content versioning via a content-addressed shadow-git store.

ADR-0038 Stage 1d (D9). The runtime side of a generation lives in
``SnapshotGenerationStore`` (per-agent ``AgentSnapshot`` by WAL seq); the
*workspace files* are the other substrate and live here. A generation ties the
two at a boundary seq:

    generation(N) = {per-agent AgentSnapshot @ N}  ⊗  {shadow-git commit @ N}

This store is the workspace half: ONE content-addressed shadow-git repo (the
workspace is a single SSoT — ADR D2) keyed by the GLOBAL WAL seq via tags
``reyn-gen-<seq>``. Unchanged files are de-duplicated across generations (git
content-addressing) so cutting a generation at every boundary is cheap.

Design (Claude Code / Cline style):
- The shadow repo's ``git-dir`` lives under the OS state dir
  (``.reyn/workspace-shadow.git``) — OUT of the tracked work-tree.
- The ``work-tree`` is the workspace root. Every git invocation passes explicit
  ``--git-dir`` / ``--work-tree`` — we never write a ``.git`` file into the
  work-tree, so the shadow coexists with a user's own git repo untouched.
- ``.reyn/`` is excluded (via ``<git-dir>/info/exclude``) so OS state (WAL,
  snapshots, this shadow repo) is never tracked nor wiped on restore.

``capture`` / ``restore_at_or_below`` mirror ``SnapshotGenerationStore.record``
/ ``nearest_at_or_below`` + ``load`` so ``rewind_to`` drives both substrates the
same way. When ``git`` is unavailable the store degrades to logged no-ops —
runtime rewind still works; only workspace-file rewind is skipped.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_TAG_PREFIX = "reyn-gen-"
_TAG_RE = re.compile(rf"^{re.escape(_TAG_PREFIX)}(\d+)$")
# Marker committer identity — shadow commits never touch the user's git identity.
_COMMITTER = ("REYN_SHADOW_NAME", "reyn-shadow", "REYN_SHADOW_EMAIL", "reyn@shadow.local")


class WorkspaceVersionStore:
    """Content-addressed shadow-git store for workspace files (ADR-0038 1d).

    Parameters
    ----------
    workspace_root:
        The work-tree captured/restored (= the agent workspace base dir).
    git_dir:
        Where the shadow repo lives (e.g. ``.reyn/workspace-shadow.git``).
        Kept out of the work-tree so OS state is never tracked.
    exclude:
        Paths (relative to the work-tree) the shadow never tracks. Defaults to
        ``[".reyn/"]`` so OS state (WAL / snapshots / this repo) is excluded.
    """

    def __init__(
        self,
        workspace_root: Path,
        git_dir: Path,
        *,
        exclude: list[str] | None = None,
    ) -> None:
        self._work_tree = Path(workspace_root)
        self._git_dir = Path(git_dir)
        self._exclude = list(exclude) if exclude is not None else [".reyn/"]

    # ── availability ──────────────────────────────────────────────────────

    @staticmethod
    def git_available() -> bool:
        """True iff a ``git`` binary is on PATH (else the store no-ops)."""
        return shutil.which("git") is not None

    # ── capture / restore ─────────────────────────────────────────────────

    def capture(self, seq: int) -> str | None:
        """Capture the current workspace as the generation tagged ``seq``.

        ``add -A`` + commit + tag ``reyn-gen-<seq>``. Idempotent per seq: if the
        tag already exists (the global seq was captured by a peer agent's
        boundary), returns the existing commit sha without re-committing.
        Returns the commit sha, or ``None`` when git is unavailable.
        """
        if not self.git_available():
            logger.debug("git unavailable — workspace capture(%s) skipped", seq)
            return None
        self._ensure_repo()
        existing = self._tag_sha(self._tag(seq))
        if existing is not None:
            return existing
        self._git("add", "-A")
        # --allow-empty: a boundary with no file change still gets a generation
        # so every seq is restorable (matches the runtime snapshot at that seq).
        self._git(
            "commit", "--allow-empty", "-q", "-m", f"reyn generation @ seq {seq}",
        )
        self._git("tag", self._tag(seq))
        return self._rev_parse("HEAD")

    def restore_at_or_below(self, seq: int) -> str | None:
        """Restore the workspace to the nearest generation with tag-seq <= ``seq``.

        ``reset --hard`` to that commit + ``clean -fd`` (honoring the excludes)
        so files added after the generation are removed while excluded OS state
        (``.reyn/``) survives. Returns the restored commit sha, or ``None`` when
        git is unavailable or no generation at-or-below ``seq`` exists.
        """
        if not self.git_available():
            logger.debug("git unavailable — workspace restore(%s) skipped", seq)
            return None
        if not self._git_dir.exists():
            return None
        base = self._nearest_at_or_below(seq)
        if base is None:
            return None
        tag = self._tag(base)
        self._git("reset", "--hard", "-q", tag)
        clean_args = ["clean", "-fdq"]
        for pat in self._exclude:
            clean_args += ["-e", pat]
        self._git(*clean_args)
        return self._tag_sha(tag)

    # ── queries ──────────────────────────────────────────────────────────

    def seqs(self) -> list[int]:
        """Sorted list of captured generation seqs (from ``reyn-gen-*`` tags)."""
        if not self.git_available() or not self._git_dir.exists():
            return []
        out = self._git("tag", "--list", f"{_TAG_PREFIX}*")
        found = []
        for line in (out or "").splitlines():
            m = _TAG_RE.match(line.strip())
            if m:
                found.append(int(m.group(1)))
        return sorted(found)

    def prune_below(self, min_keep_seq: int) -> int:
        """Delete generations with seq < ``min_keep_seq`` (retention; wired in 1e).

        Returns the number of generation tags removed. Blob GC (``git gc``) is
        left to the retention sweep (Stage 1e) so a prune is cheap here.
        """
        removed = 0
        for s in self.seqs():
            if s < min_keep_seq:
                self._git("tag", "-d", self._tag(s))
                removed += 1
        return removed

    # ── internals ──────────────────────────────────────────────────────────

    def _tag(self, seq: int) -> str:
        return f"{_TAG_PREFIX}{int(seq)}"

    def _nearest_at_or_below(self, seq: int) -> int | None:
        candidates = [s for s in self.seqs() if s <= seq]
        return max(candidates) if candidates else None

    def _ensure_repo(self) -> None:
        """Initialise the shadow repo (idempotent) + write the exclude file."""
        if not (self._git_dir / "HEAD").exists():
            self._git_dir.mkdir(parents=True, exist_ok=True)
            self._git("init", "-q")
        # Per-repo excludes live in <git-dir>/info/exclude — no tracked
        # .gitignore, nothing written into the work-tree.
        info = self._git_dir / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "exclude").write_text(
            "".join(f"{pat}\n" for pat in self._exclude), encoding="utf-8",
        )

    def _git(self, *args: str) -> str:
        name_k, name_v, email_k, email_v = _COMMITTER
        cmd = [
            "git",
            "--git-dir", str(self._git_dir),
            "--work-tree", str(self._work_tree),
            # Pin a shadow identity so commits never depend on (or touch) the
            # user's global git config.
            "-c", f"user.name={name_v}",
            "-c", f"user.email={email_v}",
            *args,
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True,
        )
        return result.stdout

    def _rev_parse(self, ref: str) -> str | None:
        try:
            return self._git("rev-parse", ref).strip() or None
        except subprocess.CalledProcessError:
            return None

    def _tag_sha(self, tag: str) -> str | None:
        try:
            out = self._git("rev-parse", "--verify", "-q", f"{tag}^{{commit}}")
        except subprocess.CalledProcessError:
            return None
        return out.strip() or None
