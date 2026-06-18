"""Random helpers — explicit seeding only (no ambient global RNG)."""

from __future__ import annotations

import random as _random


def seeded(seed: int) -> _random.Random:
    """Return a fresh ``random.Random`` seeded with ``seed``.

    The returned RNG is independent of the global ``random`` module
    state; reusing the same seed across calls reproduces the same
    sequence (= deterministic under replay).
    """
    return _random.Random(seed)
