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

    kept: list[Any] = []
    total = 0
    for region in valid:
        matches = region.get("matches")
        if isinstance(matches, list) and len(matches) > _MAX_MATCHES_PER_REGION:
            region = {**region, "matches": matches[:_MAX_MATCHES_PER_REGION]}
        size = len(json.dumps(region))
        if total + size > _MAX_TOTAL_CHARS:
            break  # PRIMARY bound reached — output is bounded by construction
        total += size
        kept.append(region)

    return {**inner, "_plan_regions": kept}
