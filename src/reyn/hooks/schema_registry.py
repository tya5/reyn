"""reyn.hooks.schema_registry — the code-shipped Hook-Event Schema Registry
(Hook-Event Redesign Phase 1, proposal
``docs/deep-dives/proposals/0059-hook-event-redesign.md`` §4).

Two-layer split (proposal §4 review-pass — the v0.2 draft's IN/OUT-set
contradiction, fixed): this module is the **builtin layer** — code-shipped,
versioned with reyn itself, and the operator CANNOT edit it. (An operator
**extension** layer for webhook-provider schemas / a per-``event_name``
``llm:*`` allowlist is a later phase — OUT-set, ``reyn.yaml``-only,
restart-only. Not built here.)

``is_emittable_llm_kind`` below (Hook-Event Redesign Phase 5 part 2, §8.4
item 3) fills the STRUCTURAL half of that reserved OUT-set slot NOW: a
static, code-shipped KIND-SHAPE whitelist for the ``emit_hook_event`` op
(``reyn.core.op_runtime.emit_hook_event``) — the only kind an LLM may ever
emit is its OWN session's ``llm:<session_id>:*`` namespace; every other
namespace (``builtin:*``/``composed:*``/``webhook:*``/``mcp:*``, and any
OTHER session's ``llm:*``) is rejected. This is a shape gate, not (yet) a
per-``event_name`` value allowlist — the future OUT-set config file layers
name-level curation ON TOP of this structural gate, it does not replace it.

``BUILTIN_HOOK_SCHEMAS`` is the single source of truth for what field-set
each of reyn's builtin hook-points carries — mirroring the
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
alias** for the builtin 8 — existing ``hooks.yaml`` configs written before
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
    "session_start", "session_end", "turn_start", "turn_end",
)
_EXTERNAL_POINTS: "tuple[str, ...]" = (
    "mcp_resource_updated", "file_changed", "cron_fired", "webhook_received",
)
# proposal 0067 P3 — neither a session lifecycle transition nor an external
# ingress signal; a THIRD category for "an async unit of work reached a
# terminal disposition" (today: only the pipeline-async terminal-delivery
# producer — see build_hook_payload's own P3 note below for why delegate_to_
# agent's chain-resolve path is deliberately NOT a second producer yet).
_TASK_POINTS: "tuple[str, ...]" = ("task_settled",)

BARE_TO_KIND: "dict[str, str]" = {
    **{p: f"builtin:lifecycle:{p}" for p in _LIFECYCLE_POINTS},
    **{p: f"builtin:external:{p}" for p in _EXTERNAL_POINTS},
    **{p: f"builtin:task:{p}" for p in _TASK_POINTS},
}
KIND_TO_BARE: "dict[str, str]" = {kind: bare for bare, kind in BARE_TO_KIND.items()}


def canonical_kind(point: str) -> str:
    """Normalize a bare short-form (``"turn_end"``) OR an already-canonical
    namespaced kind (``"builtin:lifecycle:turn_end"``) to the canonical
    namespaced kind. An unrecognised ``point`` (a non-builtin / future /
    test-only point) is returned UNCHANGED — the schema-driven open set: only
    the shipped builtin points (``BARE_TO_KIND``) are known here, everything
    else is simply unvalidated (not an error)."""
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
    # #5221: sensitive_op_count / sensitive_op_kinds_csv are the behavioral-
    # anomaly-detector's closed-vocabulary data source (see
    # reyn.runtime.turn_behavior_tally) — a `pipeline_launch` hook on this
    # point can read them via `input_template`. Every value either field can
    # ever carry is drawn from SENSITIVE_OP_KINDS, itself a fixed subset of
    # AUDIT_EVENT_KINDS — never raw message text.
    "builtin:lifecycle:turn_end": frozenset(
        {"point", "agent_name", "chain_id", "user_text",
         "sensitive_op_count", "sensitive_op_kinds_csv"},
    ),
    "builtin:external:mcp_resource_updated": frozenset(
        {"point", "server", "uri", "agent_name", "resync"},
    ),
    "builtin:external:file_changed": frozenset({"point", "path", "event_type"}),
    # #5209: "action" — "message" (default, also delivered a message to the
    # inbox) or "hook" (fired cron_fired only, no message) — lets an
    # on: cron_fired hook branch on which kind of fire this was.
    "builtin:external:cron_fired": frozenset({"point", "job_name", "to", "action"}),
    "builtin:external:webhook_received": frozenset({"point", "transport", "sender"}),
    # proposal 0067 P3/P4e: TWO producers — the pipeline-async terminal-
    # delivery path (P3, owner ruling via lead-coder, 2026-08-10) and
    # run_prompt(collect="async")'s settle branch (P4e, #3978, landed —
    # InterAgentMessaging.handle_agent_response's kind="prompt" branch).
    # Both dispatch through the SAME schema/kind, no new one added.
    # delegate_to_agent's own chain-resolve completion never folds in
    # here — architect ruling, #3978: P6 retired the tool with no
    # replacement producer, so its chains stay outside the task/settle
    # vocabulary permanently (kind=None for their remaining lifetime).
    "builtin:task:task_settled": frozenset(
        {"point", "task_id", "kind", "status", "session", "result"},
    ),
}

# The schema-driven OPEN SET of valid builtin ``on:`` values — the single
# source ``reyn.hooks.schema.ALLOWED_HOOK_POINTS`` derives from (bare form,
# the form config + HookDef/HookRegistry/dispatch use internally).
ALLOWED_HOOK_KINDS: "frozenset[str]" = frozenset(BUILTIN_HOOK_SCHEMAS)


# ---------------------------------------------------------------------------
# context_safe (proposal 0067 § "The gate, before the field that needs it",
# P2) — per-field, per-kind: is this field safe to interpolate into a hook
# push's message template (``reyn.hooks.render.render_push``)?
# ---------------------------------------------------------------------------
#
# ``CONTEXT_UNSAFE_FIELDS`` is a SEPARATE structure from
# ``BUILTIN_HOOK_SCHEMAS`` (owner ruling via lead-coder, broker 2026-08-10)
# rather than folding a per-field bool into that dict's value type — the
# alternative measured at 10 src/tests files (8 direct
# ``frozenset(payload) == BUILTIN_HOOK_SCHEMAS[kind]`` asserts + 2
# ``monkeypatch.setitem`` sites) that would all need rewriting for a type
# change carrying no new information for those tests (they check field-set
# membership, not safety). This structure is untouched by that blast radius.
#
# DENY-LIST, not allow-list, and the empty default is deliberate: it is the
# owner's standing policy made structural — "UX・予測可能性優先、
# セキュリティは opt-in" (permanent instruction) means a NEW field defaults
# to context_safe (interpolatable), and only a field someone has decided is
# unsafe gets listed here. All 8 of today's builtin schemas' fields are
# safe (owner ruling, 2026-08-10) — the empty default below is not a
# placeholder, it is the CURRENT real state, matching #4069's own
# "explicit reviewed allowlist over open-ended widening" shape (this is the
# same pattern inverted: an explicit reviewed DENYLIST over open-ended
# narrowing).
#
# Sync-drift risk (only one direction is dangerous — a stale/typo'd field
# name here silently stays "safe" rather than wrongly excluding something,
# since the render-side filter only REMOVES names it finds a match for):
# closed by ``tests/hooks/test_context_safe_gate_3978.py``'s own
# membership check (``CONTEXT_UNSAFE_FIELDS[kind] <= BUILTIN_HOOK_SCHEMAS[kind]``
# for every kind), not by convention.
CONTEXT_UNSAFE_FIELDS: "dict[str, frozenset[str]]" = {
    # proposal 0067 P3, ADR-0040 D3: `result` is LLM-authored content (the
    # task's own output) — the FIRST field this deny-list actually excludes.
    # Not covered by the "current fields are safe" owner ruling (that
    # covered the 8 pre-P3 builtin schemas only): P3's OWN design already
    # specifies "result: context_safe false" (proposal §"The task
    # mechanism"), so this entry is the payload's authors' declared intent,
    # not a narrowing added after the fact.
    "builtin:task:task_settled": frozenset({"result"}),
}


def safe_context_fields(kind_or_point: str, context: dict) -> dict:
    """*context* filtered down to fields safe for hook-push MESSAGE
    interpolation (``reyn.hooks.render.render_push``) — every key in
    *context* except the ones ``CONTEXT_UNSAFE_FIELDS`` names for this
    kind. A kind with no entry (today: 8 of the 9 builtins — every one
    except ``builtin:task:task_settled``) removes nothing —
    ``dict.get(kind, frozenset())`` is the empty-deny-list default (see
    module docstring above).

    This does NOT validate *context* against ``BUILTIN_HOOK_SCHEMAS`` —
    that is ``validate_payload``'s job, at payload-construction time, not
    render time; this function only filters whatever it is handed."""
    kind = canonical_kind(kind_or_point)
    unsafe = CONTEXT_UNSAFE_FIELDS.get(kind, frozenset())
    if not unsafe:
        return context
    return {k: v for k, v in context.items() if k not in unsafe}


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


# ---------------------------------------------------------------------------
# LLM-emit OUT-set whitelist (§8/§8.4 item 3, Hook-Event Redesign Phase 5
# part 2) — the KIND dimension of the emit_hook_event autonomy boundary.
# ---------------------------------------------------------------------------

_LLM_KIND_PREFIX = "llm:"


def is_emittable_llm_kind(kind: str, session_id: str) -> bool:
    """OUT-set whitelist gate for the ``emit_hook_event`` op (proposal §8.4
    item 3): ``True`` iff ``kind`` is exactly this ``session_id``'s OWN
    ``llm:<session_id>:<event_name>`` namespace (a non-empty ``event_name``
    suffix required).

    Enforced BY THE HANDLER (``reyn.core.op_runtime.emit_hook_event``)
    BEFORE ``HookBus.publish`` — ``HookBus.publish`` is synchronous, never
    raises, and broadcasts to every live subscriber (``reyn.hooks.bus``), so
    there is no downstream gate once an event reaches the bus; this
    function is the ONLY defense line.

    Everything else is REJECTED, deliberately, by omission — this is a
    static allowlist, not a denylist, so a future namespace addition to
    ``reyn.hooks.schema_registry``/the Kind Namespace (proposal §2) does
    NOT silently become emittable:
      - ``builtin:*`` — spoofs Reyn's own lifecycle/ingress events.
      - ``composed:*`` — spoofs a Composer's CORRELATED output; an LLM
        forging this would let it fire a ``composed:*``-only hook (e.g. an
        approval-gated deploy) WITHOUT the Composer's actual correlation
        logic ever running — a privilege-escalation shortcut around the
        entire point of Composer (``reyn.hooks.composer`` invariant #5:
        only a Composer ever calls ``HookBus.publish``).
      - ``webhook:*`` / ``mcp:*`` — spoofs external ingress sources.
      - another session's ``llm:<OTHER-session-id>:*`` — cross-session
        emission; ``session_id`` here is ALWAYS ``OpContext.session_id``
        (never LLM-supplied — see ``EmitHookEventIROp``'s docstring for the
        structural session-binding this depends on), so this branch only
        matters if a caller ever passes a session_id it does not itself
        own, which the handler never does.
    """
    if not session_id:
        return False
    prefix = f"{_LLM_KIND_PREFIX}{session_id}:"
    return kind.startswith(prefix) and len(kind) > len(prefix)


__all__ = [
    "ALLOWED_HOOK_KINDS",
    "BARE_TO_KIND",
    "BUILTIN_HOOK_SCHEMAS",
    "CONTEXT_UNSAFE_FIELDS",
    "KIND_TO_BARE",
    "HookSchemaError",
    "bare_point",
    "build_hook_payload",
    "is_emittable_llm_kind",
    "canonical_kind",
    "safe_context_fields",
    "validate_payload",
]
