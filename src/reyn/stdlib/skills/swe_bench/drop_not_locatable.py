"""Phase preprocessor (#1216, #1209 follow-up): drop not-locatable edits.

#1209 PR-B grounds each edit in a deterministic ``grep`` region (``_edit_regions``,
one entry per edit in plan order). A no-match anchor yields a region with
``count == 0`` (escape_anchors' ``(?!)`` sentinel / an anchor absent from the
file). The apply instructions told the model to *skip* a count-0 edit — but
that is instruction-side and **compliance-dependent**: in #1216 run2 the model
ignored it and blind-edited from the non-existent anchor anyway (0/4, empty patch).

This step closes that path **structurally / deterministically** (P5, the
``deterministic_split`` care boundary — shape the environment, don't rely on the
weak model honouring a rule): it runs AFTER the iterate-grep and partitions the
edit plan by its region's match count —

  * ``count > 0`` (locatable, one or many matches) → kept in ``edits`` (actionable).
  * ``count == 0`` (or no region at all) → **removed from ``edits``** so the model
    has no anchored region to blind-edit from, and **recorded in ``not_locatable``**
    so the gap is recoverable downstream (partial patch → verify surfaces it →
    re-plan, #1204 territory) instead of silently lost.

Pure data transform (no file access; sandboxed python-step). Deterministic,
never LLM-mutated. Writes the whole inner data via ``into: data`` (it produces
two outputs — the filtered ``edits`` and the new ``not_locatable`` list).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _count(region: Any) -> int | None:
    """Return a region's match count, or None when the region is malformed/absent."""
    if isinstance(region, dict):
        c = region.get("count")
        return c if isinstance(c, int) else None
    return None


def drop_not_locatable(data: Mapping[str, Any]) -> dict:
    """Partition ``edits`` by ``_edit_regions`` match count; drop+record count-0.

    Receives the FULL artifact (edit plan at ``data["data"]["edits"]`` with a flat
    ``data["edits"]`` fallback for unit tests, mirroring escape_anchors). Returns
    the new inner data dict (write back with ``into: data``): ``edits`` keeps only
    locatable edits; ``not_locatable`` records the dropped ones.

    Alignment: ``_edit_regions`` is built by iterating over the (post-escape)
    ``edits`` in plan order — one entry per edit — so ``edits[i]`` pairs with
    ``regions[i]``. An edit with no region (index past the regions list) or a
    malformed/zero count is treated as not-locatable (the safe default: no
    confirmed in-context region ⇒ not actionable).
    """
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    edits = inner.get("edits") or []
    regions = inner.get("_edit_regions") or []

    # Filter ``edits`` AND ``_edit_regions`` in LOCKSTEP. apply.md Step 1 pairs
    # ``edits[i] ↔ _edit_regions[i]`` by plan-order index, so dropping a mid-plan
    # not-locatable edit from ``edits`` alone would shift every surviving edit
    # onto the wrong region (a locatable edit could inherit a dropped count-0
    # region and look not-locatable). Keep the two lists index-aligned by
    # appending an actionable edit's region only when the edit is kept.
    actionable_edits: list[Any] = []
    actionable_regions: list[Any] = []
    not_locatable: list[Any] = list(inner.get("not_locatable") or [])
    for i, edit in enumerate(edits):
        region = regions[i] if i < len(regions) else None
        c = _count(region)
        if c and c > 0:  # count is a positive int
            actionable_edits.append(edit)
            actionable_regions.append(region)
        else:
            not_locatable.append(edit)

    return {
        **inner,
        "edits": actionable_edits,
        "_edit_regions": actionable_regions,
        "not_locatable": not_locatable,
    }
