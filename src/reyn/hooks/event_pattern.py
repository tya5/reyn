"""reyn.hooks.event_pattern — the typed ``EventPattern`` match grammar
(Hook-Event Redesign Phase 3, proposal
``docs/deep-dives/proposals/0059-hook-event-redesign.md`` §10 Q-reyn-4).

Generalizes the pre-Phase-3 ``HookDef.matcher`` (a ``field -> pattern`` dict
evaluated against a hook-event's payload, ``reyn.hooks.matcher``) into a typed
``EventPattern`` with THREE independent predicates over a ``HookEvent``:

- ``kind``:    exact match against ``HookEvent.kind`` (bare or namespaced
               form both accepted, normalized via
               ``schema_registry.canonical_kind`` before comparing).
- ``source``:  exact match against ``HookEvent.source``. **On the
               ``HookBus`` (Hook-Event Redesign Phase 4a/4b), ``source`` is
               DEGENERATE — every event ``HookDispatcher.dispatch`` builds
               carries ``source="builtin"``, always, because ``kind``
               already encodes the source TYPE (``mcp_resource_updated`` vs
               ``file_changed`` vs ...) and ``payload`` retains the source
               INSTANCE (``payload.server`` / ``payload.path`` /
               ``payload.job_name`` / ``payload.transport``). Correlate on
               the payload INSTANCE field, never on ``source`` — a pattern
               (or a Composer input, ``reyn.hooks.composer``) written as
               ``source="mcp:github"`` can never match anything the Bus
               carries and is a silent-never-fire footgun, the same class
               Phase 3's schema validation closed for payload field typos.
- ``payload``: the SAME ``field -> pattern`` dict semantics as the legacy
               matcher (``reyn.hooks.matcher.matches``) — exact string
               equality per field, except ``uri``/``path`` which glob
               (``fnmatch``); a field named in ``payload`` that's ABSENT from
               the event's payload never matches; an absent/empty ``payload``
               predicate always matches.

Backward-compat (byte-identical, critical)
-------------------------------------------
Every existing ``HookDef.matcher`` dict becomes a payload-only ``EventPattern``
(``kind=None``, ``source=None``) via :func:`from_legacy_matcher` — its
kind/source predicates are unset (always match), and its payload predicate is
evaluated by calling ``reyn.hooks.matcher.matches`` directly (NOT
reimplemented here), so the result is IDENTICAL to the pre-Phase-3
``HookDispatcher.dispatch`` matcher check for every existing ``hooks.yaml``
entry. No migration is needed; ``reyn.hooks.matcher`` itself is UNCHANGED and
stays the single source of truth for payload-field predicate semantics
(exact/glob/absent/empty) — this module only adds the kind/source layer
around it.

Static validation (the NEW capability — §4, Q-reyn-4's "typo-resistance")
---------------------------------------------------------------------------
:func:`validate_against_schema` checks an ``EventPattern``'s payload field
NAMES against ``schema_registry.BUILTIN_HOOK_SCHEMAS`` for a given kind,
flagging a field the kind's schema does not carry (e.g. ``payload.srever`` on
``mcp_resource_updated``, whose real field is ``server``).

This is ENFORCED AT LOAD, not opt-in (proposal §4 Q-reyn-4 architect ruling):
``reyn.hooks.loader.load_hooks`` calls it for every configured matcher, so a
schema-external matcher field is a fail-loud ``HookConfigError`` at config
load instead of a silent "never fire" footgun (a ``srever`` typo matches
nothing at dispatch, and the operator gets no signal it was a typo). This is
additive correctness: a schema-VALID matcher still parses + evaluates
byte-identically; only a schema-EXTERNAL (dead) matcher now surfaces at load.

Open set preserved: a ``kind`` with NO builtin schema entry (a future /
non-builtin point) is a silent no-op here (nothing to validate against) — the
same "open set" posture as ``schema_registry.validate_payload``, so a
schema-driven future point remains permissive.
"""
from __future__ import annotations

from dataclasses import dataclass

from reyn.hooks.event import HookEvent
from reyn.hooks.matcher import matches as _payload_matches
from reyn.hooks.schema_registry import BUILTIN_HOOK_SCHEMAS, HookSchemaError, canonical_kind


@dataclass(frozen=True)
class EventPattern:
    """A typed match predicate over a :class:`~reyn.hooks.event.HookEvent`
    (``kind`` / ``source`` / ``payload``).

    All three predicates are optional and independently applied (AND
    semantics — every set predicate must hold). An ``EventPattern`` with all
    three unset (the default) always matches — the SAME fire-always default
    the legacy bare matcher dict has (see module docstring)."""

    kind: "str | None" = None
    source: "str | None" = None
    payload: "dict[str, str] | None" = None


def from_legacy_matcher(matcher: "dict[str, str] | None") -> EventPattern:
    """Wrap a pre-Phase-3 ``HookDef.matcher`` dict as a payload-only
    ``EventPattern`` — ``kind``/``source`` unset, so its evaluation via
    :func:`matches` is byte-identical to the legacy
    ``reyn.hooks.matcher.matches(matcher, payload)`` call."""
    return EventPattern(payload=matcher)


def matches(pattern: "EventPattern | None", event: HookEvent) -> bool:
    """Return whether ``event`` satisfies ``pattern``.

    ``pattern`` is ``None`` -> always ``True`` (unset predicates default to
    always-match, generalizing the legacy None/empty-matcher default from
    ``reyn.hooks.matcher.matches``).
    """
    if pattern is None:
        return True
    if pattern.kind is not None and canonical_kind(pattern.kind) != canonical_kind(event.kind):
        return False
    if pattern.source is not None and pattern.source != event.source:
        return False
    # Payload predicate — delegates to the EXACT legacy function (no
    # reimplementation), so behavior is byte-identical to pre-Phase-3 matcher
    # evaluation for every existing hooks.yaml matcher dict.
    return _payload_matches(pattern.payload, event.payload)


def validate_against_schema(pattern: EventPattern, kind: str) -> None:
    """Raise ``HookSchemaError`` iff ``pattern.payload`` names a field that
    ``kind``'s builtin schema (``schema_registry.BUILTIN_HOOK_SCHEMAS``) does
    not carry — typo-resistance (e.g. ``payload.srever`` vs the real
    ``server`` field). A ``kind`` with no builtin schema entry (a future /
    non-builtin point — the schema-driven open set) is a silent no-op —
    nothing to validate against, the same "open set" posture as
    ``schema_registry.validate_payload``.

    ENFORCED AT LOAD: ``reyn.hooks.loader.load_hooks`` calls this for every
    configured matcher (wrapping a ``HookSchemaError`` into a decision-enabling
    ``HookConfigError``), so a schema-external matcher fails loud at config
    load rather than silently never firing (see module docstring)."""
    if not pattern.payload:
        return
    schema = BUILTIN_HOOK_SCHEMAS.get(canonical_kind(kind))
    if schema is None:
        return
    unknown = sorted(set(pattern.payload) - schema)
    if unknown:
        raise HookSchemaError(
            f"EventPattern payload field(s) {unknown} not in {canonical_kind(kind)!r}'s "
            f"builtin schema ({sorted(schema)}) — check for a typo."
        )


__all__ = ["EventPattern", "from_legacy_matcher", "matches", "validate_against_schema"]
