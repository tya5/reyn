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
import time
from pathlib import Path

# OS default seed: 5 universal + 5 Reyn flagship actions.
# Referenced by ActionRetrievalConfig when hot_list_seed="default".
DEFAULT_HOT_LIST_SEED: tuple[str, ...] = (
    "file__read",
    "file__grep",
    "web__search",
    "web__fetch",
    "memory.operation__remember_shared",
    "skill__skill_builder",
    "skill__skill_improver",
    "skill__skill_importer",
    "skill__mcp_search",
    "skill__read_local_files",
)

_SECONDS_PER_DAY = 86400.0


class ActionUsageTracker:
    """Freq+recency tracker for the FP-0034 hot list.

    Thread-safety: single-process / single-thread only (= router context).
    No locking is applied; callers must not share instances across threads.
    """

    def __init__(self, persist_path: Path | None = None) -> None:
        # in-memory state
        self._freq: dict[str, int] = {}       # qualified_name → event count
        self._last_ts: dict[str, float] = {}  # qualified_name → latest event timestamp
        self._persist_path = persist_path
        if persist_path is not None:
            self._load_from_disk()

    # ── Disk persistence helpers ──────────────────────────────────────────

    def _load_from_disk(self) -> None:
        """Load JSONL history into in-memory state.

        Swallows all errors and falls back to empty state — persistence
        failure is non-fatal; the tracker functions correctly in memory-only
        mode after a failed load.
        """
        if self._persist_path is None or not self._persist_path.exists():
            return
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
        """
        ts = time.time()
        self._freq[qualified_name] = self._freq.get(qualified_name, 0) + 1
        prev = self._last_ts.get(qualified_name)
        if prev is None or ts > prev:
            self._last_ts[qualified_name] = ts
        self._append_to_disk(qualified_name, ts)

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
