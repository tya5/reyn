"""#4206 ② — the bounding axis: agent/session layers may narrow a ceiling,
never widen it. Owner motivation (issue body): ``model`` is not a
"preference" — it consumes a shared, bounded resource (the process-shared
``BudgetTracker``'s daily/monthly quota), so a free-override composition
(③, ``reyn.runtime.preferences``) is the wrong shape: a child that could
freely override would let it exhaust the parent's own budget.

## Why a SEPARATE declaration from ``PREFERENCE_KEYS``

Architect's own measurement (#4206 comment thread, #4726 co-vet): mixing ②
into ``PREFERENCE_KEYS`` would make the composition function branch per-key
(③ = "last one present wins"; ② = "narrowest one present wins") — the SAME
"same name, two different mechanisms" trap #4726's Design C already closed
for ③ itself (a live-property key vs. a caller-resolved key). ``BOUNDING_KEYS``
keeps the SAME flat-dict-declaration shape ``PREFERENCE_KEYS`` established
(a typo'd/renamed key raises loudly rather than silently doing nothing —
the #4655 defect class), with only the composition function swapped.

## Scope: ``model`` only (#4206 ②, lead-coder ruling 2026-08-14)

Architect measured that ``timeout``/``router_max_iterations`` are NOT ready
for this axis: ``router_max_iterations`` has no config key at all today
(only a hardcoded default), and no driving reason was presented for
opening ``timeout`` to agent/session layers. Both stay OUT of
``BOUNDING_KEYS`` until a real need is measured — adding an unused key
here would repeat the exact "declared but nobody reads it" shape #4655
closed for config-schema leaves.

## Composition: narrowest wins, restrict-only

Unlike ③'s free-override ("last one present wins", no ceiling check), ②'s
composition is a MINIMUM over ``STANDARD_CLASSES``' own ascending-cost
order (light < standard < strong) — a layer's declared value can only
LOWER the effective ceiling relative to layers above it, never raise it.
``model_class_exceeds_ceiling`` (``model_resolver.py``) is the existing
predicate this axis's enforcement already uses at the #1190 chokepoint
(``recorded_acompletion``); this module only adds the LAYER composition
that predicate consumes — the ordering, the predicate, and the enforcement
chokepoint are #4206 T1's, already implemented, unchanged.
"""
from __future__ import annotations

from reyn.llm.model_resolver import STANDARD_CLASSES

#: The ONE key #4206 ②'s current scope covers — see the module docstring
#: for why ``timeout``/``router_max_iterations`` are not (yet) here.
BOUNDING_KEYS: "frozenset[str]" = frozenset({"model"})


class UnknownBoundingKeyError(ValueError):
    """A ``bounding:`` mapping (agent ``profile.yaml`` or session
    ``config.yaml``) names a dotted key outside :data:`BOUNDING_KEYS` —
    raised loudly rather than silently ignored, the same discipline
    :class:`reyn.runtime.preferences.UnknownPreferenceKeyError` established
    for the ③ axis."""


class InvalidBoundingValueError(ValueError):
    """A ``bounding.model`` value is not one of ``STANDARD_CLASSES`` —
    raised loudly at the SAME entry point :class:`UnknownBoundingKeyError`
    guards, for the same reason (lead-coder review, #4727): unlike
    ``model_class_exceeds_ceiling``'s own "not comparable -> ignore"
    scope limit (a LOCAL, single-judgment deferral), letting a typo'd
    value (``"strogn"``) reach :func:`compose_model_ceiling` silently
    drops that LAYER's ceiling from the composition entirely — if every
    other layer is also unset, the composed result is ``None``
    (unbounded), turning "restrict to standard" into "no restriction at
    all" with no exception and no warning. Closed at the validation
    entry point, not by adding a branch to the composition function."""


def validate_bounding(bounding: "dict[str, object]", *, source: str) -> None:
    """Raise :class:`UnknownBoundingKeyError` for any key in *bounding* that
    is not in :data:`BOUNDING_KEYS`, and :class:`InvalidBoundingValueError`
    for a ``model`` value outside ``STANDARD_CLASSES``. *source* names
    where this mapping came from (an agent name or a session id) so the
    error is actionable."""
    unknown = set(bounding) - BOUNDING_KEYS
    if unknown:
        raise UnknownBoundingKeyError(
            f"{source}: unrecognized bounding key(s) {sorted(unknown)!r} — "
            f"not in BOUNDING_KEYS ({sorted(BOUNDING_KEYS)!r}). A typo'd "
            f"or renamed bounding key would otherwise silently fail to "
            f"narrow anything."
        )
    if "model" in bounding and bounding["model"] not in STANDARD_CLASSES:
        raise InvalidBoundingValueError(
            f"{source}: bounding.model={bounding['model']!r} is not one of "
            f"STANDARD_CLASSES ({list(STANDARD_CLASSES)!r}). A typo'd or "
            f"stale class name would otherwise silently fail to narrow "
            f"anything (compose_model_ceiling ignores an incomparable "
            f"value), which can silently WIDEN the effective ceiling to "
            f"unbounded if no other layer sets one."
        )


def compose_model_ceiling(*ceilings: "str | None") -> "str | None":
    """②'s composition rule for the ONE ordered key this axis covers so
    far: the NARROWEST (lowest ``STANDARD_CLASSES`` index) of *ceilings*,
    ignoring ``None`` entries (a layer that declared no ceiling never
    widens the effective one — the restrict-only guarantee).

    A ceiling value outside :data:`reyn.llm.model_resolver.
    STANDARD_CLASSES` is NOT comparable on this axis (mirrors
    ``model_class_exceeds_ceiling``'s own "can only judge violations it can
    actually order" scope limit) and is ignored rather than raised HERE —
    this is defense-in-depth for the one caller this function does NOT
    validate first (the PROJECT layer, ``resolver.class_ceiling()``, which
    predates ② and is validated by ``ModelResolver`` itself, not
    ``validate_bounding``). The agent/session layers ARE validated before
    they ever reach this function (:func:`validate_bounding` raises
    :class:`InvalidBoundingValueError` at the entry point instead) — an
    incomparable value from an agent/session layer should never actually
    arrive here; a project-layer value slipping through uncomparable still
    degrades to "this layer doesn't narrow", not a raise, matching
    ``model_class_exceeds_ceiling``'s own local scope limit for THAT one
    layer. Returns ``None`` when every layer is ``None``/incomparable
    (fully unbounded, the compat default)."""
    comparable = [c for c in ceilings if c in STANDARD_CLASSES]
    if not comparable:
        return None
    return min(comparable, key=STANDARD_CLASSES.index)


__all__ = [
    "BOUNDING_KEYS",
    "InvalidBoundingValueError",
    "UnknownBoundingKeyError",
    "compose_model_ceiling",
    "validate_bounding",
]
