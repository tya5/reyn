"""Append-only op-granular workspace content log (ADR-0038 #1560 act-turn).

The per-boundary shadow-git generations (``WorkspaceVersionStore``) capture the
workspace at turn / plan-step boundaries. Act-turn rewind targets a finer seq —
a ``step_completed`` boundary *inside* a skill run — below the nearest boundary
generation. This log fills that gap: when the opt-in ``time_travel.act_turn_capture``
is on, each ``step_completed`` records ``(op_seq, tree_sha)`` where ``tree_sha`` is
a bare ``write-tree`` snapshot in the shadow store's object set (no commit/tag).

``op_seq`` is the WAL seq of the ``step_completed`` event (= ``CommittedStep.seq``),
so one rewind ``target_seq`` restores both the runtime memo (≤ K) and the workspace
tree (≤ K). Restore (read the latest entry ≤ target → ``read-tree``) is wired in
PR-2; this module is the capture-side log.

Recovery state (keyed by WAL seq), so it lives beside the shadow git-dir (routed
under ``--state-dir`` with it, #1557) — **not** the audit EventStore.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class WorkspaceOpContentLog:
    """Append-only ``(op_seq, tree_sha)`` JSONL log beside the shadow git-dir.

    Append is best-effort (a failed line never breaks the caller — it is on the
    swallow-safe post-append observer path). Reads tolerate torn/garbage lines
    (skipped), mirroring the WAL replay tolerance.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, op_seq: int, tree_sha: str) -> None:
        """Append one ``{op_seq, tree_sha}`` entry. Best-effort (logs + swallows)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps({"op_seq": int(op_seq), "tree_sha": str(tree_sha)}) + "\n"
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:  # noqa: BLE001 — never fail the caller (observer path)
            logger.warning(
                "op-content-log append failed (op_seq=%s): %s", op_seq, e,
            )

    def entries(self) -> list[dict]:
        """All ``{op_seq, tree_sha}`` entries in file order; [] if absent. Garbage
        lines are skipped (torn-write tolerance, like WAL replay)."""
        if not self._path.is_file():
            return []
        out: list[dict] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and "op_seq" in rec and "tree_sha" in rec:
                    out.append(rec)
        return out

    def latest_tree_at_or_below(self, seq: int) -> str | None:
        """The ``tree_sha`` of the highest ``op_seq <= seq`` (None if none).

        Restore-side convenience (PR-2 reads this); is_active resolution is the
        caller's responsibility (the log itself is branch-agnostic — capture is
        always current-branch, lineage is resolved at restore)."""
        best_seq = -1
        best_sha: str | None = None
        for rec in self.entries():
            s = rec.get("op_seq")
            if isinstance(s, int) and best_seq < s <= seq:
                best_seq = s
                best_sha = rec.get("tree_sha")
        return best_sha


__all__ = ["WorkspaceOpContentLog"]
