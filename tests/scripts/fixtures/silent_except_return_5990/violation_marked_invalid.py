"""#5990 stage 2-2 fixture: a marker IS present, but names a category
outside the fixed 5-slug vocabulary — must be flagged, with a DIFFERENT
message than "no marker found" (a typo/invented reason is not the same
failure as never having tried)."""
from __future__ import annotations


def compute_key(payload: object) -> str:
    try:
        canonical = repr(payload)
    except Exception:
        # SILENT-EXCEPT-RETURN-OK: made-up-reason -- not in the vocabulary
        canonical = "fallback"
    return canonical
