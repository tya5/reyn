"""reyn.hooks.registry — in-memory hook registry (#1800 slice A).

``HookRegistry`` stores ``HookDef`` objects in registration order and
exposes a single query method: ``hooks_for(point)`` returns the ordered
list of hooks registered for a given lifecycle point.

Registration order is preserved (list semantics, not dict/set).  Multiple
hooks at the same point are run in the order they appear in reyn.yaml.
"""
from __future__ import annotations

from reyn.hooks.schema import HookDef


class HookRegistry:
    """Ordered store of ``HookDef`` objects, queryable by hook-point.

    Parameters
    ----------
    defs:
        Ordered sequence of validated ``HookDef`` objects (typically
        produced by ``load_hooks``).  Stored in the order given.
    """

    def __init__(self, defs: list[HookDef]) -> None:
        self._defs: list[HookDef] = list(defs)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def hooks_for(self, point: str) -> list[HookDef]:
        """Return all hooks registered for *point* in registration order.

        Parameters
        ----------
        point:
            A lifecycle hook-point name (e.g. ``"turn_end"``).  Unknown
            points return an empty list (no error) — forward-compat for
            new points not yet in ``ALLOWED_HOOK_POINTS``.

        Returns
        -------
        list[HookDef]
            Possibly empty; order matches the order in reyn.yaml.
        """
        return [d for d in self._defs if d.on == point]

    # ------------------------------------------------------------------
    # Dunder helpers (for tests / repr)
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._defs)

    def __repr__(self) -> str:  # pragma: no cover
        return f"HookRegistry({self._defs!r})"
