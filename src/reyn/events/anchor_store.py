"""Per-checkpoint anchor-text store for the rewind timeline (ADR-0038 #1547).

The TUI ``/rewind`` timeline (1f) shows each checkpoint as ``seq · rel-time ·
kind``. This store adds the *content* anchor — the truncated last user message
at that checkpoint — captured at ``cut_generation`` time (the ``history_buffer``
holds it in-memory, so it is cheap, robust, and survives independent of audit-log
rotation). It is the **correct source** (vs mining the audit EventStore, which has
no WAL seq and would need a cross-log join — see #1547 rationale).

One global store keyed by the single global WAL seq (the same key the timeline +
generations use), so surfacing an anchor is a trivial seq lookup with no cross-log
correlation. Additive-only: nothing in the 1a–1e generation contract changes.
``prune_below`` is GC'd on the same boundary as Stage 1e retention.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 80


def truncate_anchor(text: str, *, limit: int = _DEFAULT_LIMIT) -> str:
    """Collapse to a single line + truncate to ``limit`` chars (ellipsis if cut)."""
    one_line = " ".join(text.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 1].rstrip() + "…"


class AnchorStore:
    """Maps a WAL checkpoint seq → its anchor text (truncated last user message).

    JSON-backed (``{seq: text}``); anchors are tiny and bounded by the retention
    window. ``get`` returns ``""`` for an unknown seq so callers can slot the
    field in unconditionally.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._anchors: dict[int, str] | None = None

    def _load(self) -> dict[int, str]:
        if self._anchors is None:
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                self._anchors = {int(k): str(v) for k, v in raw.items()}
            except FileNotFoundError:
                self._anchors = {}
            except (OSError, ValueError) as e:
                logger.warning("anchor store load failed (%s): %s", self._path, e)
                self._anchors = {}
        return self._anchors

    def _save(self) -> None:
        anchors = self._load()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({str(k): v for k, v in anchors.items()}),
            encoding="utf-8",
        )

    def capture(self, seq: int, text: str) -> None:
        """Record the anchor ``text`` for checkpoint ``seq`` (idempotent overwrite)."""
        if not text:
            return
        self._load()[int(seq)] = text
        self._save()

    def get(self, seq: int) -> str:
        """Return the anchor for ``seq``, or ``""`` when none is recorded."""
        return self._load().get(int(seq), "")

    def prune_below(self, min_keep_seq: int) -> int:
        """Drop anchors with seq < ``min_keep_seq`` (Stage 1e retention GC)."""
        anchors = self._load()
        drop = [s for s in anchors if s < min_keep_seq]
        for s in drop:
            del anchors[s]
        if drop:
            self._save()
        return len(drop)
