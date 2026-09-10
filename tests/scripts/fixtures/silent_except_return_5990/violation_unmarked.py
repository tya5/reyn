"""#5990 stage 2-2 fixture: an assign-only except handler whose bound name
reaches this function's own `return`, with NO marker — the violation-side
shape `check_silent_except_return_reachability.py` must flag."""
from __future__ import annotations


def compute_key(payload: object) -> str:
    try:
        canonical = repr(payload)
    except Exception:
        canonical = "fallback"
    return canonical
