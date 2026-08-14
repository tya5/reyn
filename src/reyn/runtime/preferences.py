"""#4206 slice 1 — the ③ preference axis: free-override, non-capability,
non-bounding config that an operator (any layer) or a parent session
(session layer only) may set, always winning over the project default with
NO restriction/ceiling check — unlike ① capability (restrict-only, ∩) and
② bounding (child narrows only, ceiling stays with the operator).

Composition rule (lead-coder ruling, #4206): project default -> agent-layer
override (if present) -> session-layer override (if present); the LAST one
present wins outright. There is no "child cannot widen past parent" check
here — that's what makes this axis ③, not ②.

## PREFERENCE_KEYS — a hand-maintained declaration, not derived

The set below is the ONE place this axis's key vocabulary is declared.
Lead-coder ruling: a typed dataclass per key was rejected (breaks "the
mechanism stays the same as ③ grows" — #4206's own explicit requirement),
so this is a flat ``dict[str, object]`` keyed by dotted config-path string
instead. That shape alone would reproduce the #4655 "silent no-op" defect
class (a typo'd/renamed key silently doing nothing forever) — closed the
same way #4655 closed it for config-schema dict-leaves: :func:`validate_preferences`
raises LOUDLY on any key not in this set, rather than accepting and
ignoring it.

**Known, accepted tradeoff (lead-coder, explicit)**: this IS "one more
declaration" to keep in sync as the ③ set grows — #4206's own classification
of which config keys belong to which axis is not yet derivable from code
(it lives in the issue's own prose), and making it derivable is a SEPARATE
future task, deliberately not mixed into this slice.
"""
from __future__ import annotations

#: The 8 keys #4206's confirmed classification places in axis ③ for this
#: first slice: ``output_language``, ``chat.reasoning.display``, one
#: ``warn_ratio`` per of the 6 ``CostLimitConfig`` dimensions
#: (``runtime/budget/budget.py``'s ``CostConfig``), and
#: ``cost.rate_limit_warn_ratio``. Every entry is a dotted path matching
#: the SAME key names ``reyn.yaml`` uses for the project-level default
#: (``config/chat.py``'s own parsing), so an agent/session override reads
#: as "the same setting, one layer down" — not a renamed shadow key.
PREFERENCE_KEYS: "frozenset[str]" = frozenset({
    "output_language",
    "chat.reasoning.display",
    "cost.per_agent_tokens.warn_ratio",
    "cost.per_agent_cost_usd.warn_ratio",
    "cost.daily_tokens.warn_ratio",
    "cost.daily_cost_usd.warn_ratio",
    "cost.monthly_tokens.warn_ratio",
    "cost.monthly_cost_usd.warn_ratio",
    "cost.rate_limit_warn_ratio",
})


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
