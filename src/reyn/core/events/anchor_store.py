"""Per-checkpoint anchor-text store for the rewind timeline (ADR-0038 #1547).

The TUI ``/rewind`` timeline (1f) shows each checkpoint as ``seq · rel-time ·
kind``. This store adds the *content* anchor — the truncated last human
prompt (CLIENT_INPUT-origin; for a non-human turn, the most recent one
before it — #5648) as of that checkpoint — captured at ``cut_generation``
time (the ``history_buffer``
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

# #6265: the 2c edit-prefill cap on ``full`` — owner decision 2026-09-27. A
# normal turn's message never approaches this; a large paste is cut here (the
# primary copy stays intact in history.jsonl, #6042-capped — ``full`` is only
# a convenience copy for re-editing, so cutting it loses no record).
_FULL_LIMIT = 4000


def truncate_anchor(text: str, *, limit: int = _DEFAULT_LIMIT, one_line: bool = True) -> str:
    """Truncate to ``limit`` chars (ellipsis if cut), collapsing to a single line
    unless ``one_line=False``.

    The 1f rewind-timeline preview (``anchor``) always wants ``one_line=True`` —
    it renders inline next to other timeline columns. The 2c edit-prefill copy
    (``full``) wants ``one_line=False``: it re-populates the input box for
    editing, where the original line breaks are part of what the user typed and
    collapsing them would corrupt the text being handed back for edit — cutting
    length is a bound on file size, not a license to reformat the message.
    """
    source = " ".join(text.split()) if one_line else text
    if len(source) <= limit:
        return source
    return source[: limit - 1].rstrip() + "…"


class AnchorStore:
    """Maps a WAL checkpoint seq → its anchor text (the truncated last
    human prompt — CLIENT_INPUT-origin; for a non-human turn, the most
    recent one before it, #5648).

    JSON-backed; each entry is ``{"anchor": <truncated display>, "full": <the
    original user message, itself truncated to ``_FULL_LIMIT`` chars — #6265>,
    "full_truncated": <bool>}``. The ``anchor`` drives the rewind-timeline preview
    (1f) and the ``full`` message is the source for the 2c edit-prefill (#1533
    2c). Both are captured at ``cut_generation`` time, where the full message is
    in hand — robust vs fragile after-the-fact WAL/history mining.

    ``anchor`` and ``full`` are cut by the SAME mechanism (``truncate_anchor``,
    #6265 — before this, ``full`` was stored with no cap at all, so one large
    paste made every subsequent turn rewrite an unbounded ``full`` field). Per
    entry, ``anchor`` is bounded by ``_DEFAULT_LIMIT`` (80 chars) and ``full`` by
    ``_FULL_LIMIT`` (4,000 chars, owner decision 2026-09-27); the file as a whole
    is bounded by that per-entry cap times however many seqs the retention
    window (``prune_below``) keeps — NOT by entry size alone, since count also
    varies.

    ``get`` / ``get_full`` return ``""`` for an unknown seq so callers can slot the
    field in unconditionally. ``full_truncated`` lets a caller (the 2c
    edit-prefill) tell a cut copy from a complete one WITHOUT re-deriving it from
    length (the primary message is #6042-capped in ``history.jsonl`` regardless,
    so a cut ``full`` loses no record — only the convenience re-edit). **Back-
    compatible**: a pre-2c file stores ``{seq: <str>}``; such values load as
    ``{"anchor": <str>, "full": "", "full_truncated": False}`` so the display
    still works and the edit-prefill degrades to empty (manual re-type).
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._anchors: dict[int, dict[str, object]] | None = None

    @staticmethod
    def _normalize(value: object) -> dict[str, object]:
        """Coerce a stored value to ``{"anchor", "full", "full_truncated"}``
        (legacy str → full="", full_truncated=False)."""
        if isinstance(value, dict):
            return {
                "anchor": str(value.get("anchor", "")),
                "full": str(value.get("full", "")),
                "full_truncated": bool(value.get("full_truncated", False)),
            }
        return {"anchor": str(value), "full": "", "full_truncated": False}   # pre-2c str value

    def _load(self) -> dict[int, dict[str, object]]:
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
        the 2c edit-prefill (defaults empty for callers that have only the anchor);
        cut to ``_FULL_LIMIT`` chars by the SAME ``truncate_anchor`` mechanism as
        ``text`` (#6265 — ``one_line=False``, so line breaks in the original
        message survive the cut). ``full_truncated`` records whether the cut
        actually fired, so a caller can tell a complete copy from a cut one
        without re-deriving it from length.
        """
        if not text:
            return
        full_truncated = len(full) > _FULL_LIMIT
        self._load()[int(seq)] = {
            "anchor": text,
            "full": truncate_anchor(full, limit=_FULL_LIMIT, one_line=False),
            "full_truncated": full_truncated,
        }
        self._save()

    def get(self, seq: int) -> str:
        """Return the truncated display anchor for ``seq``, or ``""`` when none."""
        entry = self._load().get(int(seq))
        return str(entry["anchor"]) if entry else ""

    def get_full(self, seq: int) -> str:
        """Return the full original message for ``seq`` (2c edit-prefill source),
        cut to ``_FULL_LIMIT`` chars (#6265).

        ``""`` when none recorded or when the entry predates 2c (legacy str value)
        — the edit-prefill then degrades to empty (manual re-type). Call
        ``full_truncated(seq)`` to tell whether THIS return value was cut — the
        2c edit-prefill consumer needs that to show the user their edit will not
        include the original's tail (owner decision 2026-09-27: silent cutting is
        not allowed).
        """
        entry = self._load().get(int(seq))
        return str(entry["full"]) if entry else ""

    def full_truncated(self, seq: int) -> bool:
        """Whether ``get_full(seq)`` is a cut copy of the original message (#6265).

        The 2c edit-prefill consumer calls this alongside ``get_full`` to decide
        whether to tell the user their re-edit will not cover the original's
        tail. ``False`` for an unknown seq or a pre-2c legacy entry (nothing was
        cut because nothing full-length was ever stored).
        """
        entry = self._load().get(int(seq))
        return bool(entry["full_truncated"]) if entry else False

    def prune_below(self, min_keep_seq: int) -> int:
        """Drop anchors with seq < ``min_keep_seq`` (Stage 1e retention GC)."""
        anchors = self._load()
        drop = [s for s in anchors if s < min_keep_seq]
        for s in drop:
            del anchors[s]
        if drop:
            self._save()
        return len(drop)
