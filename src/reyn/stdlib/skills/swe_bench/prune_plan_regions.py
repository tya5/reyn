"""Phase preprocessor (#1366 follow-up): bound the plan-time region volume.

The plan preprocessor greps each problem-statement symbol against the explore
``relevant_files`` and collects the grepped regions into ``_plan_regions``. But a
common symbol (e.g. ``Column`` in astropy/table/table.py) matches *hundreds* of
lines, and with surrounding context each such grep result is enormous — the raw
``_plan_regions`` reached ~6 MB on astropy-13236, overwhelming the plan model's
context so it aborts. That defeats the whole purpose (the truncation fix) by
re-introducing context bloat at the plan layer.

This step runs AFTER the iterate-grep and makes the surfaced volume **bounded by
construction** (the [[feedback_bounded_by_construction_pattern]]):

  * **Primary bound = a size budget.** Each region's ``matches`` are capped to
    ``_MAX_MATCHES_PER_REGION`` and regions are added only while the running total
    stays under ``_MAX_TOTAL_CHARS``. So ``_plan_regions`` is *always* small,
    whatever the grep counts — the context cannot bloat regardless of input.
  * **Secondary signal = match count, used only to RANK.** A region with fewer
    matches is a more *specific* locator (the symbol pinpoints a place), so we add
    regions in ascending-count order: the precise locators (the gold target
    regions) are surfaced first and a non-specific high-count region (``Column``
    everywhere) is naturally crowded out by the budget rather than by a magic
    threshold. ``count == 0`` (symbol absent) is dropped as a validity filter
    (a no-match region has no locating value), not as the bound.

When everything is dropped (all symbols absent) or the budget is tiny,
``_plan_regions`` ends up small/empty and the plan model falls back to its own
targeted reads (graceful, the same as the no-symbol case). Pure data transform
(no file access; sandboxed python-step), deterministic, never LLM-mutated; writes
the whole inner data via ``into: data``.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

# PRIMARY bound (bounded-by-construction): the total serialized size of the
# surfaced _plan_regions never exceeds this — the context is small whatever the
# grep counts are.
_MAX_TOTAL_CHARS = 40000
# PRIMARY bound: per-region cap on how many matches are kept (a few sample
# regions ground an anchor; the model can issue a targeted read for more).
_MAX_MATCHES_PER_REGION = 3


def prune_plan_regions(data: Mapping[str, Any]) -> dict:
    """Return the inner data dict with ``_plan_regions`` bounded by a size budget,
    precise (low-match-count) regions prioritized (write back with ``into: data``).

    Receives the FULL artifact (regions at ``data["data"]["_plan_regions"]`` with a
    flat fallback for unit tests, mirroring the other swe_bench preprocessors)."""
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    regions = inner.get("_plan_regions") or []

    # validity filter: a region must have at least one match to locate anything.
    valid = [
        r
        for r in regions
        if isinstance(r, dict) and isinstance(r.get("count"), int) and r["count"] >= 1
    ]
    # SECONDARY signal: rank by specificity — fewer matches = more precise locator,
    # surfaced first so the gold target regions win the size budget over a
    # non-specific high-count symbol.
    valid.sort(key=lambda r: r["count"])

    # #1375 D1: within a region, select which matches to keep by PROXIMITY to
    # other symbols' matches in the same file, not by first-N. The gold fix site
    # tends to cluster where several problem-symbols co-occur (a method that
    # references multiple of them), whereas the dropped junk matches are isolated
    # early lines (module docstring / imports). Keeping the co-located matches
    # surfaces the gold region; first-N kept only the early lines (astropy-13453:
    # plain `write` matched @[2,5,15] — all early — and dropped the gold @349).
    # `all_marks` = every match's (file, line) across the (count-sorted) regions,
    # tagged with the region index so a region's own matches are excluded.
    all_marks: list[tuple[str, int, int]] = []
    for i, r in enumerate(valid):
        for m in r.get("matches") or []:
            p, ln = _match_path(m), _match_line(m)
            if p is not None and ln is not None:
                all_marks.append((p, ln, i))

    def _proximity(m: Any, region_index: int) -> float:
        """Min distance from this match to any OTHER region's match in the same
        file. inf when isolated (no co-occurring symbol nearby) → ranked last."""
        p, ln = _match_path(m), _match_line(m)
        if p is None or ln is None:
            return float("inf")
        dists = [abs(ln - L) for (P, L, R) in all_marks if P == p and R != region_index]
        return min(dists) if dists else float("inf")

    kept: list[Any] = []
    total = 0
    for i, region in enumerate(valid):
        matches = region.get("matches")
        if isinstance(matches, list) and len(matches) > _MAX_MATCHES_PER_REGION:
            # keep the matches closest to other symbols' matches (gold cluster)
            ranked = sorted(matches, key=lambda m: _proximity(m, i))
            region = {**region, "matches": ranked[:_MAX_MATCHES_PER_REGION]}
        size = len(json.dumps(region))
        if total + size > _MAX_TOTAL_CHARS:
            break  # PRIMARY bound reached — output is bounded by construction
        total += size
        kept.append(region)

    # #1375 D8: drop the plan-time intermediates so the plan model's context
    # carries only the final `_plan_regions` (the anchor-grounding material).
    # `_explore_symbols` / `_symbol_files` / `_candidate_files` / `_plan_symbols`
    # are scaffolding the preprocessor used to BUILD the regions (and on the first
    # plan `_candidate_files` is computed but unused); leaving them in context is
    # noise for the plan model.
    pruned = {k: v for k, v in inner.items()
              if k not in ("_explore_symbols", "_symbol_files",
                           "_candidate_files", "_plan_symbols",
                           "_filename_tokens", "_filename_files")}
    pruned["_plan_regions"] = kept
    return pruned


def _match_line(m: Any) -> int | None:
    """A grep match's 1-based line number (the data stores it as a string)."""
    if isinstance(m, dict):
        try:
            return int(m.get("line_number"))
        except (TypeError, ValueError):
            return None
    return None


def _match_path(m: Any) -> str | None:
    """A grep match's file path (each match carries its own ``path``)."""
    if isinstance(m, dict):
        p = m.get("path")
        return p if isinstance(p, str) and p else None
    return None
