"""Time helpers (ambient sources — outputs are non-deterministic)."""

from __future__ import annotations

import time as _time


def monotonic_seq() -> float:
    """Return a monotonic clock value in seconds.

    Non-deterministic: two calls within the same process produce
    different values. Wraps ``time.monotonic``. Use only when the
    step's contract permits ambient clock reads (= replay records
    the value).
    """
    return _time.monotonic()
