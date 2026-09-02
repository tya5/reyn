"""#4206 slice 1 — the ③ preference axis: free-override, non-capability,
non-bounding config that an operator (any layer) or a parent session
(session layer only) may set, always winning over the project default with
NO restriction/ceiling check — unlike ① capability (restrict-only, ∩) and
② bounding (child narrows only, ceiling stays with the operator).

Composition rule (lead-coder ruling, #4206): project default -> agent-layer
override (if present) -> session-layer override (if present); the LAST one
present wins outright. There is no "child cannot widen past parent" check
here — that's what makes this axis ③, not ②.

## PREFERENCE_KEYS — DERIVED from field metadata (#4206, architect ruling
## 2026-09-02T04:01)

Was a hand-maintained declaration (9 keys, typed out by hand here); now
derived from ``metadata={"axis": Axis.PREFERENCE, "override_enabled":
True}`` on the owning ``ReynConfig`` leaf's own field declaration —
:func:`~reyn.config.config_schema.walk_config_schema` is the SAME
canonical enumeration ``reyn config fields``/``get``/``set`` already
use, so this set can no longer drift from what the schema itself says
(closing the #4655 "silent no-op" defect class one level up: a
hand-typed SECOND copy of a fact the schema already declares).

**The CONTENT is derived; the COMPOSITION MECHANISM is not** (architect,
same ruling): a typed dataclass per key was rejected (breaks "the
mechanism stays the same as ③ grows"), so :func:`validate_preferences`/
:func:`compose_preferences` below are unchanged — only the SOURCE of
this one set moved.

**``override_enabled`` is a SEPARATE, narrower flag from the axis
itself** — classifying a leaf ``Axis.PREFERENCE`` is not the same claim
as "this leaf has a live ``preferences:`` override receptacle". Adding
a new leaf's axis metadata does NOT put it in this set; only a field
ALSO carrying ``"override_enabled": True`` is (see
``reyn.config_axis.Axis``'s own module docstring for the full
reasoning — the same distinction ``runtime/bounding.py`` makes for
``BOUNDING_KEYS``). Today that is exactly the 9 pre-existing members
this set already had by hand: ``output_language``,
``chat.reasoning.display``, one ``warn_ratio`` per of the 6
``CostLimitConfig`` dimensions (``runtime/budget/budget.py``), and
``cost.rate_limit_warn_ratio`` — this migration changes WHERE that fact
is declared, not WHICH leaves currently have a receptacle.
"""
from __future__ import annotations


def _derive_preference_keys() -> "frozenset[str]":
    """Every ``ReynConfig`` leaf whose field declares BOTH
    ``axis=Axis.PREFERENCE`` and ``override_enabled=True`` — see this
    module's own docstring for why the axis classification alone is not
    enough. Imports lazily (module-level import would pull
    ``config_schema`` → ``ReynConfig`` at ``reyn.runtime.preferences``
    import time — this module has no other reason to need the full
    config tree that early)."""
    from reyn.config.config_schema import walk_config_schema
    from reyn.config_axis import Axis
    return frozenset(
        node.key for node in walk_config_schema()
        if node.axis == Axis.PREFERENCE and node.override_enabled
    )


#: See :func:`_derive_preference_keys` — the 9 keys #4206's confirmed
#: classification places in axis ③ with a live override receptacle:
#: ``output_language``, ``chat.reasoning.display``, one ``warn_ratio``
#: per of the 6 ``CostLimitConfig`` dimensions, and
#: ``cost.rate_limit_warn_ratio``. Every entry is a dotted path matching
#: the SAME key names ``reyn.yaml`` uses for the project-level default
#: (``config/chat.py``'s own parsing), so an agent/session override reads
#: as "the same setting, one layer down" — not a renamed shadow key.
PREFERENCE_KEYS: "frozenset[str]" = _derive_preference_keys()


class UnknownPreferenceKeyError(ValueError):
    """A ``preferences:`` mapping (agent ``profile.yaml`` or session
    ``config.yaml``) names a dotted key outside :data:`PREFERENCE_KEYS` —
    raised loudly rather than silently ignored, so a typo'd or
    since-renamed preference key cannot silently do nothing forever (the
    same defect class #4655 closed for config-schema dict-leaves)."""


def validate_preferences(preferences: "dict[str, object]", *, source: str) -> None:
    """Raise :class:`UnknownPreferenceKeyError` for any key in *preferences*
    that is not in :data:`PREFERENCE_KEYS`. *source* names where this
    mapping came from (e.g. an agent name or a session id) so the error is
    actionable. A caller with an EMPTY ``preferences: {}`` (the common case
    — most agents/sessions set none) never reaches this check's failure
    path at all, only its no-op success one."""
    unknown = set(preferences) - PREFERENCE_KEYS
    if unknown:
        raise UnknownPreferenceKeyError(
            f"{source}: unrecognized preference key(s) {sorted(unknown)!r} — "
            f"not in PREFERENCE_KEYS ({sorted(PREFERENCE_KEYS)!r}). A typo'd "
            f"or renamed preference key would otherwise silently do nothing."
        )


def resolve_preference(
    key: str,
    default: object,
    *,
    agent_preferences: "dict[str, object] | None" = None,
    session_preferences: "dict[str, object] | None" = None,
) -> object:
    """Free-override resolution for ONE ③ preference *key*: *default* (the
    project-level value), then the agent-layer override if *key* is present
    in *agent_preferences*, then the session-layer override if *key* is
    present in *session_preferences* — the LAST one present wins outright,
    no restriction/ceiling check (see module docstring for why this axis is
    ③, not ②).

    *key* MUST be a member of :data:`PREFERENCE_KEYS` — an unknown key
    raises :class:`UnknownPreferenceKeyError` rather than silently always
    returning *default* (which would look identical to "no override was
    ever set" for a caller who mistyped the key)."""
    if key not in PREFERENCE_KEYS:
        raise UnknownPreferenceKeyError(
            f"resolve_preference: {key!r} is not in PREFERENCE_KEYS "
            f"({sorted(PREFERENCE_KEYS)!r})"
        )
    value = default
    if agent_preferences and key in agent_preferences:
        value = agent_preferences[key]
    if session_preferences and key in session_preferences:
        value = session_preferences[key]
    return value


__all__ = [
    "PREFERENCE_KEYS",
    "UnknownPreferenceKeyError",
    "resolve_preference",
    "validate_preferences",
]
