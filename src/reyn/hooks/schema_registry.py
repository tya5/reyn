"""reyn.hooks.schema_registry — the code-shipped Hook-Event Schema Registry
(Hook-Event Redesign Phase 1, proposal
``docs/deep-dives/proposals/0059-hook-event-redesign.md`` §4).

Two-layer split (proposal §4 review-pass — the v0.2 draft's IN/OUT-set
contradiction, fixed): this module is the **builtin layer** — code-shipped,
versioned with reyn itself, and the operator CANNOT edit it. (An operator
**extension** layer for webhook-provider schemas / ``llm:*`` whitelists is a
later phase — OUT-set, ``reyn.yaml``-only, restart-only. Not built here.)

``BUILTIN_HOOK_SCHEMAS`` is the single source of truth for what field-set
each of reyn's 10 builtin hook-points carries — mirroring the
``OP_KIND_MODEL_MAP`` ↔ ``control-ir.md`` sync discipline (CLAUDE.md hard
rule): every dispatch call site MUST build its payload through
``build_hook_payload`` (below), which validates the assembled dict against
this table at construction time. A call site can no longer silently drift
from the schema — a missing/renamed/extra field raises immediately, at the
one place the payload is built, instead of only being discoverable by
diffing dispatch traces after the fact.

Kind Namespace (proposal §2) + bare-name aliasing
--------------------------------------------------
The canonical kind is namespaced (``builtin:lifecycle:turn_end``,
``builtin:external:cron_fired``); the pre-existing BARE point name
(``turn_end``, ``cron_fired``, ...) is a **permanent canonical short-form
alias** for the builtin 10 — existing ``hooks.yaml`` configs written before
this module existed keep working completely unmodified (``canonical_kind``/
``bare_point`` below normalize either spelling to the other; see
``reyn.hooks.loader`` for where config ``on:`` values are normalized, and
``reyn.hooks.dispatcher`` for where a dispatched ``point`` string is wrapped
into a ``HookEvent``).

Future-extensible seam (proposal §2/§11 "future point" list — pre/post_tool_use,
pipeline_start/end): adding a new builtin point is schema + one call site —
add an entry to ``BUILTIN_HOOK_SCHEMAS`` (+ the bare<->kind maps below) and a
``build_hook_payload(...)`` call at the new dispatch site. ``HookDispatcher``,
``HookRegistry``, ``EventPattern``/matcher, and every existing hook-point are
UNCHANGED by that addition — this registry is the open set that drives which
``on:`` values ``reyn.hooks.loader`` accepts (``ALLOWED_HOOK_POINTS`` in
``reyn.hooks.schema`` is derived from it, not maintained separately).
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Bare short-form <-> canonical namespaced kind (§2)
# ---------------------------------------------------------------------------

_LIFECYCLE_POINTS: "tuple[str, ...]" = (
    "session_start", "session_end", "turn_start", "turn_end", "task_start", "task_end",
)
_EXTERNAL_POINTS: "tuple[str, ...]" = (
    "mcp_resource_updated", "file_changed", "cron_fired", "webhook_received",
)

BARE_TO_KIND: "dict[str, str]" = {
    **{p: f"builtin:lifecycle:{p}" for p in _LIFECYCLE_POINTS},
    **{p: f"builtin:external:{p}" for p in _EXTERNAL_POINTS},
}
KIND_TO_BARE: "dict[str, str]" = {kind: bare for bare, kind in BARE_TO_KIND.items()}


def canonical_kind(point: str) -> str:
    """Normalize a bare short-form (``"turn_end"``) OR an already-canonical
    namespaced kind (``"builtin:lifecycle:turn_end"``) to the canonical
    namespaced kind. An unrecognised ``point`` (a non-builtin / future /
    test-only point) is returned UNCHANGED — the schema-driven open set: only
    the 10 shipped builtin points are known here, everything else is simply
    unvalidated (not an error)."""
    if point in KIND_TO_BARE:
        return point
    return BARE_TO_KIND.get(point, point)


def bare_point(kind: str) -> str:
    """The reverse of ``canonical_kind`` — the bare short-form
    ``HookDispatcher``/``HookRegistry`` use internally as the dispatch
    ``point`` string. An unrecognised ``kind`` is returned unchanged."""
    return KIND_TO_BARE.get(kind, kind)


# ---------------------------------------------------------------------------
# Builtin schemas — code-shipped (§4 2-layer split). Each entry is the frozen
# field-set of the point's payload dict, INCLUDING the "point" key every
# existing call site already carries (kept for byte-identical values — not
# semantically load-bearing, just historical). Additive-only evolution: add
# an optional field here + at its sole ``build_hook_payload`` call site;
# never rename/remove a shipped field (breaking).
# ---------------------------------------------------------------------------

BUILTIN_HOOK_SCHEMAS: "dict[str, frozenset[str]]" = {
    "builtin:lifecycle:session_start": frozenset({"point", "agent_name"}),
    "builtin:lifecycle:session_end": frozenset({"point", "agent_name"}),
    "builtin:lifecycle:turn_start": frozenset({"point", "agent_name", "kind", "chain_id"}),
    "builtin:lifecycle:turn_end": frozenset({"point", "agent_name", "chain_id", "user_text"}),
    "builtin:lifecycle:task_start": frozenset({"point", "task_id", "name", "assignee"}),
    "builtin:lifecycle:task_end": frozenset({"point", "task_id", "status"}),
    "builtin:external:mcp_resource_updated": frozenset(
        {"point", "server", "uri", "agent_name", "resync"},
    ),
    "builtin:external:file_changed": frozenset({"point", "path", "event_type"}),
    "builtin:external:cron_fired": frozenset({"point", "job_name", "to"}),
    "builtin:external:webhook_received": frozenset({"point", "transport", "sender"}),
}

# The schema-driven OPEN SET of valid builtin ``on:`` values — the single
# source ``reyn.hooks.schema.ALLOWED_HOOK_POINTS`` derives from (bare form,
# the form config + HookDef/HookRegistry/dispatch use internally).
ALLOWED_HOOK_KINDS: "frozenset[str]" = frozenset(BUILTIN_HOOK_SCHEMAS)


class HookSchemaError(ValueError):
    """A hook-event payload's field-set doesn't match its builtin schema.

    Raised by ``build_hook_payload`` at CONSTRUCTION time (the producer side)
    — every argument is a compile-time-known call-site literal, so this is a
    programming-error guard (like a pydantic validation failure), not a
    data-dependent runtime fault. It is deliberately NOT raised by
    ``HookDispatcher.dispatch()`` itself (see that module): a hook may be
    dispatched with an arbitrary/partial ``template_vars`` dict by tests and
    by future non-builtin points, and dispatch's per-hook isolation is about
    an individual HOOK's action failing, not about producer-schema drift.
    """


def validate_payload(kind_or_point: str, payload: dict) -> None:
    """Raise ``HookSchemaError`` iff ``payload``'s key-set doesn't exactly
    match the builtin schema for ``kind_or_point`` (bare or canonical form
    both accepted). A point with no builtin schema entry (open set) is a
    silent no-op — nothing to validate against."""
    kind = canonical_kind(kind_or_point)
    schema = BUILTIN_HOOK_SCHEMAS.get(kind)
    if schema is None:
        return
    actual = frozenset(payload)
    if actual != schema:
        missing = sorted(schema - actual)
        extra = sorted(actual - schema)
        raise HookSchemaError(
            f"hook-event payload for {kind!r} doesn't match its builtin schema "
            f"(missing={missing} extra={extra})."
        )


def build_hook_payload(point: str, **fields: object) -> dict:
    """Construct + validate a builtin hook-event payload — the single
    producer every dispatch call site funnels through (§4: "every dispatch
    call-site's assembled payload == the shipped schema for that point" is
    true BY CONSTRUCTION here, not just by a separate after-the-fact check).

    ``point`` may be the bare short-form or the canonical namespaced kind;
    the returned dict always carries ``"point"`` as the bare short-form
    (byte-identical to every pre-Phase-1 call-site literal). Raises
    ``HookSchemaError`` immediately if ``fields`` don't exactly match the
    point's builtin schema (minus ``"point"`` itself, which this function
    supplies)."""
    payload = {"point": bare_point(canonical_kind(point)), **fields}
    validate_payload(point, payload)
    return payload


__all__ = [
    "ALLOWED_HOOK_KINDS",
    "BARE_TO_KIND",
    "BUILTIN_HOOK_SCHEMAS",
    "KIND_TO_BARE",
    "HookSchemaError",
    "bare_point",
    "build_hook_payload",
    "canonical_kind",
    "validate_payload",
]
