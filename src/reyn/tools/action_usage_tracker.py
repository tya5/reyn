"""ActionUsageTracker — freq+recency persistence for FP-0034 hot list.

FP-0034 §D2 / §D16 spec.

Lifecycle:
  1. Construction: pass persist_path (.reyn/state/action_usage.jsonl)
     or None for in-memory only (= tests).
  2. record(qualified_name) — append event to JSONL; bump in-memory freq + recency.
  3. get_top_n(n, seed) — returns up to n qualified_names, freq+recency ranked,
     with seed items filling remaining slots.

Storage format (per line):
  {"qualified_name": "skill__code_review", "ts": 1716000000.0}

Scoring: score = freq * (1 + 1/(1+age_days))
  - freq: number of recorded events for this name
  - recency: 1/(1+age_days) where age_days = days since last event
  - Combined: freq * (1 + 1/(1+age_days)) — rewards both popular and recent
  Simple deterministic formula; no ML needed.

Seed semantics (§D16):
  - Seed items appear only when result count < n.
  - Seed items are always de-duplicated against freq-ranked items.
  - Order within seed items is preserved (= config order).

Not yet implemented:
  - Pruning / compaction of old JSONL entries (future PR)
"""
from __future__ import annotations

import json
import sys
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
    "rag.operation__drop_source",
    "memory.operation__remember_shared",
    "skill__skill_builder",
    "skill__skill_improver",
    "skill__skill_importer",
    "skill__mcp_search",
    "skill__index_docs",
    "skill__eval",
)

_SECONDS_PER_DAY = 86400.0


class ActionUsageTracker:
    """Freq+recency tracker for the FP-0034 hot list.

    Thread-safety: single-process / single-thread only (= router context).
    No locking is applied; callers must not share instances across threads.
    """

    def __init__(
        self,
        persist_path: Path | None = None,
        *,
        on_ranking_changed: Callable[[list[dict]], None] | None = None,
    ) -> None:
        # in-memory state
        self._freq: dict[str, int] = {}       # qualified_name → event count
        self._last_ts: dict[str, float] = {}  # qualified_name → latest event timestamp
        self._persist_path = persist_path
        # Issue #192: optional callback fired when the full sorted ranking
        # order changes after a record(). Caller wires this to emit a
        # ``hot_list_updated`` event so the TUI can refresh its Memory
        # tab augmentation without periodic polling. None = no callback.
        # Diff granularity is the QUALIFIED-NAME ORDER — score-only
        # changes within a stable order (e.g. the top item's freq bumps
        # but everything stays in place) do NOT fire, so the consumer
        # only sees re-rendering signals.
        self._on_ranking_changed = on_ranking_changed
        self._prior_ranking_order: list[str] | None = None
        if persist_path is not None:
            self._load_from_disk()

    # ── Disk persistence helpers ──────────────────────────────────────────

    @staticmethod
    def _is_valid_qualified_name(name: str) -> bool:
        """Return True when *name* is a structurally valid qualified action name.

        Validates that the name:
          - Contains the ``__`` separator.
          - Has a non-empty category portion that matches one of the known
            categories (= ``universal_catalog.CATEGORIES``).
          - Has a non-empty entry-name portion.

        This is a structural / parse-level check — it does not require the
        referenced skill or agent to exist at call time. The check is
        data-driven from the category registry (P7: no hardcoded ghost lists).

        Names that fail here are stale artifacts: qualified-name corruption
        (e.g. ``default_api.web__search``), unknown categories
        (e.g. ``bogus__nonexistent``), or empty entry-names.
        """
        try:
            from reyn.tools.universal_catalog import split_qualified_name
            split_qualified_name(name)
            return True
        except (ValueError, ImportError):
            return False

    def _load_from_disk(self) -> None:
        """Load JSONL history into in-memory state.

        Swallows all errors and falls back to empty state — persistence
        failure is non-fatal; the tracker functions correctly in memory-only
        mode after a failed load.

        Ghost alias rejection (B37 F1): each loaded qualified_name is
        validated via _is_valid_qualified_name. Names that fail structural
        parse (= unknown category, missing separator, qualified-name
        corruption) are silently skipped; a single warning per unique invalid
        name is printed to stderr so operators can identify stale ledger
        entries without crashing startup.
        """
        if self._persist_path is None or not self._persist_path.exists():
            return
        _warned: set[str] = set()
        try:
            with self._persist_path.open("r", encoding="utf-8") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    qn = entry.get("qualified_name")
                    ts = entry.get("ts")
                    if not isinstance(qn, str) or not qn:
                        continue
                    if not isinstance(ts, (int, float)):
                        continue
                    # Ghost alias rejection: skip names that don't parse as
                    # valid qualified names (= renamed/deleted/corrupted).
                    if not self._is_valid_qualified_name(qn):
                        if qn not in _warned:
                            print(
                                f"[reyn] action_usage: skipping invalid alias "
                                f"{qn!r} — not in current action registry",
                                file=sys.stderr,
                            )
                            _warned.add(qn)
                        continue
                    self._freq[qn] = self._freq.get(qn, 0) + 1
                    prev = self._last_ts.get(qn)
                    if prev is None or ts > prev:
                        self._last_ts[qn] = float(ts)
        except Exception:
            # Any I/O or decoding error → reset to empty state.
            self._freq = {}
            self._last_ts = {}

    def _append_to_disk(self, qualified_name: str, ts: float) -> None:
        """Append a single event line to the JSONL file.

        No-op when persist_path is None.  Swallows all errors.
        """
        if self._persist_path is None:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            entry = json.dumps(
                {"qualified_name": qualified_name, "ts": ts},
                ensure_ascii=False,
            )
            with self._persist_path.open("a", encoding="utf-8") as fh:
                fh.write(entry + "\n")
        except Exception:
            pass  # disk failure must not crash the caller

    # ── Public API ────────────────────────────────────────────────────────

    def record(self, qualified_name: str) -> None:
        """Record one usage event for *qualified_name*.

        Updates in-memory freq + recency, then appends to JSONL if
        persist_path is configured.

        Issue #192: when ``on_ranking_changed`` is set and the new full
        sorted ranking order differs from the cached prior order, the
        callback is fired with the full ranking ``[{qualified_name,
        freq, last_ts}, ...]``. Score-only changes (= same order) do
        not fire — the UI only re-renders on visible reorderings.
        Callback failures are swallowed; record() must never crash
        because the observer raised.
        """
        ts = time.time()
        self._freq[qualified_name] = self._freq.get(qualified_name, 0) + 1
        prev = self._last_ts.get(qualified_name)
        if prev is None or ts > prev:
            self._last_ts[qualified_name] = ts
        self._append_to_disk(qualified_name, ts)
        if self._on_ranking_changed is not None:
            ranking = self.full_ranking()
            new_order = [r["qualified_name"] for r in ranking]
            if new_order != self._prior_ranking_order:
                self._prior_ranking_order = new_order
                try:
                    self._on_ranking_changed(ranking)
                except Exception:
                    pass  # advisory only; never crash record()

    def full_ranking(self) -> list[dict]:
        """Return the full sorted ranking with freq + last_ts per entry.

        Sorted by the same score formula ``get_top_n`` uses (= freq *
        (1 + 1/(1+age_days))) descending, with qualified_name ascending
        as the tie-breaker. Caller consumes this for full-ranking
        rendering (= Memory tab augmentation per issue #192).
        """
        now = time.time()
        scored: list[tuple[float, str, int, float]] = []
        for qn, freq in self._freq.items():
            last = self._last_ts.get(qn, now)
            age_days = max(0.0, (now - last) / _SECONDS_PER_DAY)
            score = freq * (1.0 + 1.0 / (1.0 + age_days))
            scored.append((score, qn, freq, last))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [
            {"qualified_name": qn, "freq": freq, "last_ts": last_ts}
            for _, qn, freq, last_ts in scored
        ]

    def get_top_n(self, n: int, seed: list[str]) -> list[str]:
        """Return up to *n* qualified_names, freq+recency ranked, with seed fill.

        Algorithm:
          1. Score all recorded names: score = freq * (1 + 1/(1+age_days))
          2. Sort descending by score (ties broken by name for determinism).
          3. If result count < n, fill remaining slots from *seed* in order,
             skipping names already included (dedup).

        Returns at most *n* items.  When n <= 0, returns [].
        """
        if n <= 0:
            return []

        now = time.time()

        scored: list[tuple[float, str]] = []
        for qn, freq in self._freq.items():
            last = self._last_ts.get(qn, now)
            age_days = max(0.0, (now - last) / _SECONDS_PER_DAY)
            score = freq * (1.0 + 1.0 / (1.0 + age_days))
            scored.append((score, qn))

        # Descending score, ascending name for tie-breaking determinism.
        scored.sort(key=lambda pair: (-pair[0], pair[1]))

        result: list[str] = [qn for _, qn in scored[:n]]
        result_set: set[str] = set(result)

        if len(result) < n:
            for seed_name in seed:
                if len(result) >= n:
                    break
                if seed_name not in result_set:
                    result.append(seed_name)
                    result_set.add(seed_name)

        return result


__all__ = [
    "ActionUsageTracker",
    "DEFAULT_HOT_LIST_SEED",
]
