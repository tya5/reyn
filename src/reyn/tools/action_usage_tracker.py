"""ActionUsageTracker — compacted freq+recency table for FP-0034 hot list.

FP-0034 §D2 / §D16 spec.

Storage model
-------------

This module persists per-agent action usage as a **compacted table** of
qualified-name → ``{count, last_ts}``. The table is overwritten in
place (atomic via tmp+rename) and never grows beyond the number of
distinct qualified names ever observed.

The compacted table is fed exclusively by the chat-history compactor:
when the compactor folds N old conversation turns into a single summary
message, the tool calls contained in those folded turns are merged
into this table (one increment per call, last_ts = max).

For tool calls that have NOT yet been compacted (= still raw in
``history.jsonl``), the tracker is asked at hot-list-build time to
scan the current message list and combine them with the compacted
table. This avoids duplicating tool-call records across the conversation
history (source of truth) and the hot-list cache.

Lifecycle
---------

  1. Construction: pass ``persist_path``
     (``.reyn/agents/<name>/action_usage.json``) or ``None`` for
     memory-only operation.
  2. ``merge_compacted(records)`` — called by the compactor sink with
     the list of ``(qualified_name, ts)`` tuples extracted from
     compaction candidates. Updates the table and persists.
  3. ``get_top_n(n, seed, live_records=None)`` — returns up to *n*
     qualified names, freq+recency ranked. ``live_records`` is the
     optional list of ``(qualified_name, ts)`` extracted from the
     current uncompacted history; counts there are merged on the fly.

Storage format
--------------

JSON object::

    {
      "file__read": {"count": 12, "last_ts": 1716000000.0},
      "skill__code_review": {"count": 4, "last_ts": 1716000100.0}
    }

Scoring
-------

``score = freq * (1 + 1/(1+age_days))``

  - ``freq``: combined compacted + live count for this name.
  - ``recency``: ``1/(1+age_days)`` where ``age_days`` is days since
    the most recent observed ``last_ts``.
  - Simple deterministic formula; no ML.

Seed semantics (§D16):
  - Seed items appear only when result count < n.
  - Seed items are always de-duplicated against freq-ranked items.
  - Order within seed items is preserved (= config order).

Invalid-name filter
-------------------

Both ``merge_compacted`` and the live-scan path validate each
qualified name through :func:`_is_valid_qualified_name` (= category
prefix + ``__`` separator + non-empty entry). Wrapper invocations
(``list_actions`` / ``describe_action`` / …) and stale rename
artifacts (= bare ``read`` from before the ``file__`` prefix) are
silently dropped — they are not legitimate hot-list candidates.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

# OS default seed: universal file/web ops + Reyn flagship skills.
# Referenced by ActionRetrievalConfig when hot_list_seed="default".
# Seed growth log:
#   B27-M2: file__grep removed (no routing rule yet).
#   B27-M5: file__list + reyn.source__list added (cold-start directory listing).
#   B28-MED-1: skill__index_docs added (RAG indexing intent).
#   B30-NEW-2: skill__eval added (eval discoverability).
#   B34: file__grep + file__glob re-added (ToolDefinitions implemented).
#   B37 W4/W6: file__write + rag.operation__drop_source added (arg-canonical
#              gap — D2-wrapper scope is hot-list-only; seeding ensures schema
#              guidance is present at first use).
#   #879: skill__mcp_search → mcp__search_server; mcp__install_server added
#         as a sibling (= verb-collapse of the previous skill-space hidden
#         install action, so installation requests don't require list_actions
#         discovery first).
#   2026-05-25 (post-#898): mcp__list_tools + mcp__call_tool added (= the
#         "USE installed server" cold-start path observed missing in the
#         5-server walkthrough). skill__skill_importer + rag.operation__
#         drop_source dropped to keep seed size constant — skill_importer
#         is the niche of the three flagship skill__skill_* verbs (=
#         external import flow vs builder/improver), and drop_source's
#         B37 schema-hallucination protection is now covered by the ARS
#         scope expansion (= ``KNOWN_STATIC_QUALIFIED_NAMES`` is always in
#         ARS regardless of hot-list per the B38 contract; see
#         ``_collect_all_session_ars_entries``), so seed presence is no
#         longer load-bearing for that invariant.
DEFAULT_HOT_LIST_SEED: tuple[str, ...] = (
    "file__read",
    "file__list",
    "file__grep",
    "file__glob",
    "file__write",
    # file__edit deferred — FP-0034 §D20.
    "reyn.source__list",
    "web__search",
    "web__fetch",
    "memory.operation__remember_shared",
    "skill__skill_builder",
    "skill__skill_improver",
    "mcp__search_server",
    "mcp__install_server",
    "mcp__list_tools",
    "mcp__call_tool",
    "skill__index_docs",
    "skill__eval",
)

_SECONDS_PER_DAY = 86400.0


def _is_valid_qualified_name(name: str) -> bool:
    """Return True when *name* is a structurally valid qualified action name.

    Validates that the name parses through
    :func:`universal_catalog.split_qualified_name` — i.e. contains the
    ``__`` separator, has a category portion matching the known
    category registry, and a non-empty entry-name portion.

    Wrapper invocations (``list_actions``, ``describe_action``,
    ``search_actions``, ``invoke_action``) fail this check because they
    are not category-prefixed. Stale rename artifacts (``read`` before
    the ``file__`` prefix landed) also fail.
    """
    try:
        from reyn.tools.universal_catalog import split_qualified_name
        split_qualified_name(name)
        return True
    except (ValueError, ImportError):
        return False


class ActionUsageTracker:
    """Freq+recency tracker backed by a compacted on-disk table.

    Thread-safety: single-process / single-thread only (= router context).
    No locking is applied; callers must not share instances across threads.
    """

    def __init__(
        self,
        persist_path: Path | None = None,
        *,
        on_ranking_changed: Callable[[list[dict]], None] | None = None,
    ) -> None:
        # Compacted-table state: qn → {"count": int, "last_ts": float}
        self._compacted: dict[str, dict] = {}
        self._persist_path = persist_path
        # Issue #192: optional callback fired when the compacted ranking
        # order changes after a merge_compacted(). Caller wires this to
        # emit a ``hot_list_updated`` event so the TUI can refresh its
        # Memory-tab augmentation without periodic polling. None = no
        # callback. Diff granularity is the QUALIFIED-NAME ORDER — score-
        # only changes within a stable order do NOT fire.
        self._on_ranking_changed = on_ranking_changed
        self._prior_ranking_order: list[str] | None = None
        if persist_path is not None:
            self._load_from_disk()

    # ── Disk persistence ──────────────────────────────────────────────────

    def _load_from_disk(self) -> None:
        """Load the compacted table from JSON.

        Swallows all errors and falls back to an empty table —
        persistence failure is non-fatal; the tracker functions
        correctly in memory-only mode after a failed load.
        """
        if self._persist_path is None or not self._persist_path.exists():
            return
        try:
            raw = self._persist_path.read_text(encoding="utf-8")
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                return
            for qn, entry in obj.items():
                if not isinstance(qn, str) or not isinstance(entry, dict):
                    continue
                count = entry.get("count")
                last_ts = entry.get("last_ts")
                if not isinstance(count, int) or count <= 0:
                    continue
                if not isinstance(last_ts, (int, float)):
                    continue
                if not _is_valid_qualified_name(qn):
                    continue
                self._compacted[qn] = {
                    "count": int(count),
                    "last_ts": float(last_ts),
                }
        except Exception:
            self._compacted = {}

    def _persist_table(self) -> None:
        """Atomically rewrite ``persist_path`` with the current table."""
        if self._persist_path is None:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._persist_path.with_suffix(
                self._persist_path.suffix + ".tmp"
            )
            tmp_path.write_text(
                json.dumps(self._compacted, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(self._persist_path)
        except Exception:
            # Persistence failure is non-fatal; in-memory state is correct.
            pass

    # ── Public surface ────────────────────────────────────────────────────

    def merge_compacted(self, records: list[tuple[str, float]]) -> None:
        """Merge a batch of ``(qualified_name, ts)`` records into the
        compacted table.

        Called by the chat-compactor sink with the tool-call records
        extracted from the conversation turns being folded into a
        summary. Invalid qualified names (= wrapper invocations, stale
        rename artifacts) are silently dropped.

        Persists on every successful call; fires ``on_ranking_changed``
        when the qualified-name order of the resulting ranking
        differs from the previous one.
        """
        if not records:
            return
        changed = False
        for qn, ts in records:
            if not isinstance(qn, str) or not qn:
                continue
            if not isinstance(ts, (int, float)):
                continue
            if not _is_valid_qualified_name(qn):
                continue
            entry = self._compacted.get(qn)
            if entry is None:
                self._compacted[qn] = {"count": 1, "last_ts": float(ts)}
            else:
                entry["count"] += 1
                if ts > entry["last_ts"]:
                    entry["last_ts"] = float(ts)
            changed = True
        if not changed:
            return
        self._persist_table()
        if self._on_ranking_changed is not None:
            ranking = self.full_ranking()
            current_order = [r["qualified_name"] for r in ranking]
            if current_order != self._prior_ranking_order:
                self._prior_ranking_order = current_order
                try:
                    self._on_ranking_changed(ranking)
                except Exception:
                    pass

    def full_ranking(
        self,
        live_records: list[tuple[str, float]] | None = None,
        now: float | None = None,
    ) -> list[dict]:
        """Return the full freq+recency-ranked list of
        ``{qualified_name, freq, last_ts}`` entries.

        ``live_records`` is the optional list of ``(qualified_name, ts)``
        tuples extracted from the current uncompacted history. They are
        merged with the compacted table to produce the combined ranking.
        """
        combined: dict[str, dict] = {}
        for qn, entry in self._compacted.items():
            combined[qn] = {
                "count": entry["count"],
                "last_ts": entry["last_ts"],
            }
        if live_records:
            for qn, ts in live_records:
                if not isinstance(qn, str) or not qn:
                    continue
                if not isinstance(ts, (int, float)):
                    continue
                if not _is_valid_qualified_name(qn):
                    continue
                e = combined.get(qn)
                if e is None:
                    combined[qn] = {"count": 1, "last_ts": float(ts)}
                else:
                    e["count"] += 1
                    if ts > e["last_ts"]:
                        e["last_ts"] = float(ts)

        ref = time.time() if now is None else now
        scored: list[tuple[float, str, int, float]] = []
        for qn, e in combined.items():
            age_days = max(0.0, (ref - e["last_ts"]) / _SECONDS_PER_DAY)
            score = e["count"] * (1.0 + 1.0 / (1.0 + age_days))
            scored.append((score, qn, e["count"], e["last_ts"]))
        # Sort by score desc; break ties by qualified_name asc for
        # determinism.
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [
            {"qualified_name": qn, "freq": count, "last_ts": last_ts}
            for _, qn, count, last_ts in scored
        ]

    def get_top_n(
        self,
        n: int,
        seed: list[str],
        live_records: list[tuple[str, float]] | None = None,
    ) -> list[str]:
        """Return up to *n* qualified names: freq-ranked first, then
        seed items filling remaining slots (in seed order, deduped).
        """
        if n <= 0:
            return []
        ranked = [
            r["qualified_name"]
            for r in self.full_ranking(live_records=live_records)
        ]
        result: list[str] = ranked[:n]
        if len(result) >= n:
            return result
        existing = set(result)
        for s in seed:
            if len(result) >= n:
                break
            if s in existing:
                continue
            result.append(s)
            existing.add(s)
        return result


__all__ = [
    "DEFAULT_HOT_LIST_SEED",
    "ActionUsageTracker",
    "_is_valid_qualified_name",
]
