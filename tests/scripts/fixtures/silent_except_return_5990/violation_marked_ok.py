"""#5990 stage 2-2 fixture: the SAME violation-side shape as
`violation_unmarked.py`, but carrying a valid, vocabulary-matching marker —
must NOT be flagged."""
from __future__ import annotations


def compute_key(payload: object) -> str:
    try:
        canonical = repr(payload)
    except Exception:
        # SILENT-EXCEPT-RETURN-OK: internal-hash-fallback -- repr() is a valid fallback canonical form
        canonical = "fallback"
    return canonical
