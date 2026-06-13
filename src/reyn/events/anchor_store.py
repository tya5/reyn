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

    JSON-backed; each entry is ``{"anchor": <truncated display>, "full": <full
    original user message>}``. The truncated ``anchor`` drives the rewind-timeline
    preview (1f); the ``full`` message is the source for the 2c edit-prefill (a
    truncated re-run would lose the original tail = correctness, #1533 2c). Both
    are captured at ``cut_generation`` time, where the full message is in hand —
    robust vs fragile after-the-fact WAL/history mining. Entries are tiny and
    bounded by the retention window.

    ``get`` / ``get_full`` return ``""`` for an unknown seq so callers can slot the
    field in unconditionally. **Back-compatible**: a pre-2c file stores ``{seq:
    <str>}``; such values load as ``{"anchor": <str>, "full": ""}`` so the display
    still works and the edit-prefill degrades to empty (manual re-type).
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._anchors: dict[int, dict[str, str]] | None = None

    @staticmethod
    def _normalize(value: object) -> dict[str, str]:
        """Coerce a stored value to ``{"anchor", "full"}`` (legacy str → full="")."""
        if isinstance(value, dict):
            return {"anchor": str(value.get("anchor", "")), "full": str(value.get("full", ""))}
        return {"anchor": str(value), "full": ""}   # pre-2c str value

    def _load(self) -> dict[int, dict[str, str]]:
        if self._anchors is None:
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                self._anchors = {int(k): self._normalize(v) for k, v in raw.items()}
            except FileNotFoundError:
                self._anchors = {}
            except (OSError, ValueError) as e:
                logger.warning("anchor store load failed (%s): %s", self._path, e)
                self._anchors = {}
        return self._anchors

    def _save(self) -> None:
        anchors = self._load()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic tmp+rename: a torn write must never wipe ALL anchors (this store
        # is rewritten every turn, so torn-write risk is higher than a once-written
        # profile, and _load degrades a corrupt file to {} → the next save would
        # overwrite with {}). fsync is intentionally omitted — anchors are
        # preview-tier, not sync-durability-critical (WAL/audit separation intent).
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({str(k): v for k, v in anchors.items()}),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def capture(self, seq: int, text: str, *, full: str = "") -> None:
        """Record the anchor for checkpoint ``seq`` (idempotent overwrite).

        ``text`` = truncated display anchor (required — empty → no-op, so non-turn
        checkpoints store nothing). ``full`` = the full original user message for
        the 2c edit-prefill (defaults empty for callers that have only the anchor).
        """
        if not text:
            return
        self._load()[int(seq)] = {"anchor": text, "full": full}
        self._save()

    def get(self, seq: int) -> str:
        """Return the truncated display anchor for ``seq``, or ``""`` when none."""
        entry = self._load().get(int(seq))
        return entry["anchor"] if entry else ""

    def get_full(self, seq: int) -> str:
        """Return the full original message for ``seq`` (2c edit-prefill source).

        ``""`` when none recorded or when the entry predates 2c (legacy str value)
        — the edit-prefill then degrades to empty (manual re-type).
        """
        entry = self._load().get(int(seq))
        return entry["full"] if entry else ""

    def prune_below(self, min_keep_seq: int) -> int:
        """Drop anchors with seq < ``min_keep_seq`` (Stage 1e retention GC)."""
        anchors = self._load()
        drop = [s for s in anchors if s < min_keep_seq]
        for s in drop:
            del anchors[s]
        if drop:
            self._save()
        return len(drop)
