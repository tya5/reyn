"""Generic exponential-backoff timing — the single backoff formula shared across retry paths.

#2259 PR-2a. The LLM-call retry (``reyn.llm.llm._backoff_s``) and the durability worker's
durable-write retry (#2259 §4) need the SAME backoff curve; this is that one formula, parameter-
ised so each caller injects its own base/cap/jitter (the LLM path from its ``RetryConfig``, the
write path from the worker's bounds). One shared timing formula, no per-path reinvention.
"""
from __future__ import annotations

import random


def backoff_s(attempt: int, *, base_s: float, max_s: float, jitter: bool = True) -> float:
    """Exponential backoff with optional equal jitter, capped at ``max_s``.

    ``attempt`` is 0-indexed (attempt 0 = the first retry, after the initial call failed).
    Pure exponential is ``base_s * 2**attempt`` capped at ``max_s``. With ``jitter`` (the
    default), AWS-style equal jitter is applied: ``sleep = base/2 + uniform(0, base/2)`` — i.e.
    a value in ``[base/2, base]`` — which decorrelates concurrent retriers. ``attempt < 0`` (a
    defensive caller) yields a small non-negative wait, never a negative sleep.
    """
    exp = base_s * (2 ** attempt) if attempt >= 0 else base_s
    base = min(exp, max_s)
    if jitter:
        return base / 2 + random.uniform(0, base / 2)
    return base


__all__ = ["backoff_s"]
