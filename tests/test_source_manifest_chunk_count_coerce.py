"""Tier 2: SourceEntry.from_dict coerces a malformed chunk_count (deser-audit).

``sources.yaml`` is operator-edited. ``chunk_count=int(data.get("chunk_count", 0))``
only defaults a *missing* key, so ``chunk_count: null`` or a non-numeric value
crashed the WHOLE manifest reload (``_reload_from_file``). Coerce-to-default
closes the gap — mirrors the #1906 TokenUsage fix.

Policy: real SourceEntry, no mocks. Tier line first.
"""
from __future__ import annotations

import pytest

from reyn.data.index.source_manifest import SourceEntry


@pytest.mark.parametrize("bad", [None, "abc", "", [], {}])
def test_malformed_chunk_count_defaults_to_zero(bad) -> None:
    """Tier 2: null / non-numeric chunk_count → 0 (no TypeError/ValueError)."""
    entry = SourceEntry.from_dict("src", {"chunk_count": bad})
    assert entry.chunk_count == 0


def test_valid_chunk_count_preserved() -> None:
    """Tier 2: (regression) a valid chunk_count is unchanged; a missing key still
    defaults (the pre-existing resilience is preserved)."""
    assert SourceEntry.from_dict("src", {"chunk_count": 7}).chunk_count == 7
    assert SourceEntry.from_dict("src", {}).chunk_count == 0
