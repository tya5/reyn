"""Per-generation sqlite snapshot store (#1953 slice R).

The Task-substrate analog of ``core/events/snapshot_generations.py``'s
``SnapshotGenerationStore``: a directory of full-DB copies (``task-gen-<seq>.db``),
each a clean WAL-less sqlite file produced by ``VACUUM INTO`` at a WAL boundary
seq (the ``applied_seq`` ``SnapshotJournal.cut_generation`` keys runtime +
workspace on too).

This store holds NO lineage logic. Which generation is "active" at a rewind
target is resolved by the OS via ``is_active_seq`` (the same WAL-derived
predicate the runtime + workspace substrates use, ``snapshot_generations.py``),
so the store only manages files keyed by seq — symmetric with
``WorkspaceVersionStore`` (git tags) one layer down.
"""
from __future__ import annotations

import re
from pathlib import Path

_GEN_RE = re.compile(r"^task-gen-(\d+)\.db$")


class SqliteTaskGenerationStore:
    """Directory of ``task-gen-<seq>.db`` full-DB copies."""

    def __init__(self, gen_dir: str | Path) -> None:
        self._dir = Path(gen_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def dir(self) -> Path:
        return self._dir

    def gen_path(self, seq: int) -> Path:
        return self._dir / f"task-gen-{seq}.db"

    def has(self, seq: int) -> bool:
        return self.gen_path(seq).exists()

    def seqs(self) -> list[int]:
        """All captured generation seqs, ascending."""
        out: list[int] = []
        for p in self._dir.iterdir():
            m = _GEN_RE.match(p.name)
            if m is not None:
                out.append(int(m.group(1)))
        return sorted(out)

    def prune_below(self, min_keep_seq: int) -> int:
        """Unlink generations with ``seq < min_keep_seq`` (WAL-truncation
        piggyback) and sweep any stale ``.tmp`` files from interrupted captures.
        Returns the count of generations removed."""
        removed = 0
        for s in self.seqs():
            if s < min_keep_seq:
                self.gen_path(s).unlink(missing_ok=True)
                removed += 1
        for tmp in self._dir.glob("task-gen-*.db.tmp"):
            tmp.unlink(missing_ok=True)
        return removed
