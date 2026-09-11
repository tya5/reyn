"""Contrast fixture: THIS module calls `warnings.warn` directly, at
`stacklevel=1` (the default), while running as `__main__` itself -- the
one case Python's own stock filter DOES show
(`('default', None, DeprecationWarning, '__main__', 0)`). Exists so the
production-filter witness test proves a real difference (silent vs.
visible), not just "silent, because nothing ever shows"."""
from __future__ import annotations

import warnings

warnings.warn("visible -- this frame IS __main__", DeprecationWarning)
