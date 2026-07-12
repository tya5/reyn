"""emit_hook_event kind handler — LLM-authored hook-event emission (Hook-Event
Redesign Phase 5 part 2, proposal
``docs/deep-dives/proposals/0059-hook-event-redesign.md`` §8/§8.4).

This is the FIRST place an LLM can put a ``HookEvent`` onto a live
``HookBus`` (``reyn.hooks.bus``, Phase 4a) — every prior producer
(``HookDispatcher.dispatch`` at the 10 builtin points, ``Composer.emit``,
the Ingress Adapters) is OS-internal code, never an LLM tool call. Because
``HookBus.publish`` is synchronous, never raises, and broadcasts to every
live subscriber (module docstring, ``reyn/hooks/bus.py``), there is no
downstream gate once an event reaches the bus — THIS HANDLER is the only
defense line, and it enforces the autonomy boundary in two SEPARATE
dimensions before ever calling ``publish`` (proposal §8.4 item 3):

1. **KIND dimension** (a static OUT-set whitelist, ALLOW not DENY):
   ``reyn.hooks.schema_registry.is_emittable_llm_kind`` gates the
   constructed kind. Only this session's own ``llm:<session_id>:*``
   namespace may ever be emitted — ``builtin:*``/``composed:*``/
   ``webhook:*``/``mcp:*`` are all rejected (anti-spoofing; see that
   function's docstring for why ``composed:*`` in particular must never
   be LLM-forgeable).
2. **SESSION dimension** (STRUCTURAL for the NORMAL path, not a check to
   pass/fail): the session component of the emitted kind comes ONLY from
   ``ctx.session_id`` when using ``event_name`` (the router-tool-exposed
   field) — there is nothing there for an LLM to supply, spoof, or
   override. The ``target_kind`` escape hatch (see ``EmitHookEventIROp``'s
   docstring — NOT exposed in the router tool schema) is instead
   VALIDATED against the same whitelist; either way, this handler never
   looks up a bus by session id — ``ctx.hook_bus`` is a single fixed
   reference to THIS session's own bus, so there is no code path here
   that could route a mismatched kind's event to a different session's
   bus even absent the whitelist check.

Fail-closed throughout: a missing session identity, a whitelist miss, or
no ``HookBus`` wired all raise (never a silent no-op) — an LLM that thinks
it emitted something must never be misled about whether it actually did.
"""
from __future__ import annotations

from reyn.core.offload.canonical import emit_hook_event_to_canonical
from reyn.hooks.event import HookEvent
from reyn.hooks.schema_registry import is_emittable_llm_kind
from reyn.schemas.models import EmitHookEventIROp

from . import register
from .context import OpContext


class EmitHookEventDenied(PermissionError):
    """Raised when ``emit_hook_event`` is denied — a kind-whitelist miss, no
    bound session identity, or no ``HookBus`` wired onto this ``OpContext``.
    A ``PermissionError`` subclass so ``execute_op``'s existing
    ``except PermissionError`` branch (``op_runtime/__init__.py``) turns
    this into the standard ``{"status": "denied", ...}`` envelope + the
    existing ``permission_denied`` P6 audit-event — no new error-shape
    plumbing needed."""


async def handle(op: EmitHookEventIROp, ctx: OpContext) -> dict:
    """Execute an ``emit_hook_event`` op.

    Returns ``{"kind": "emit_hook_event", "status": "ok", "emitted_kind": str}``
    on success. Raises ``EmitHookEventDenied`` (caught by ``execute_op`` →
    ``status: "denied"``) when the session/whitelist/bus preconditions
    aren't met, or ``ValueError`` (→ ``status: "error"``) for a malformed
    ``event_name``.
    """
    session_id = ctx.session_id
    if not session_id:
        # No bound session identity — there is nothing to structurally scope
        # the emitted kind TO, so refuse outright rather than emit an
        # unscoped/ambiguous kind (fail-closed, mirrors hook_dispatcher's own
        # session_id-required cross-session-push precondition).
        raise EmitHookEventDenied(
            "emit_hook_event requires a bound session identity "
            "(OpContext.session_id) — no ad-hoc/anonymous emit is permitted."
        )

    if op.target_kind is not None:
        # Defense-in-depth escape hatch (NOT reachable from the router tool
        # schema — see EmitHookEventIROp's docstring). Whatever this value
        # is, it is gated by the EXACT same whitelist below before it can
        # ever reach ctx.hook_bus.publish.
        kind = op.target_kind.strip()
        if not kind:
            raise ValueError("target_kind must be a non-empty string when set")
    else:
        event_name = (op.event_name or "").strip()
        if not event_name:
            raise ValueError("event_name must be a non-empty string")
        # STRUCTURAL session-binding (§8.4 item 3 / co-vet ②B) for the
        # NORMAL (event_name) path: the session component of the kind is
        # built ONLY from ctx.session_id — nothing here for an LLM-supplied
        # value to override.
        kind = f"llm:{session_id}:{event_name}"

    # KIND whitelist gate (§8.4 item 3 / co-vet ②A) — enforced HERE, BEFORE
    # HookBus.publish (module docstring: publish is the point of no return).
    if not is_emittable_llm_kind(kind, session_id):
        raise EmitHookEventDenied(
            f"kind {kind!r} is not in the emittable whitelist — an LLM may "
            "only emit its own llm:<session_id>:<event_name> namespace "
            "(builtin:*/composed:*/webhook:*/mcp:* and other sessions' "
            "llm:* are never emittable)."
        )

    if ctx.hook_bus is None:
        # No HookBus wired (direct/test OpContext construction, or a
        # non-chat OpContext e.g. CLI/preprocessor) — fail closed rather
        # than silently dropping the emit.
        raise EmitHookEventDenied(
            "no HookBus wired onto this OpContext — emit_hook_event is "
            "unavailable outside a live chat session."
        )

    event = HookEvent(
        kind=kind,
        payload=dict(op.payload or {}),
        source=f"llm:{session_id}",
    )
    ctx.hook_bus.publish(event)

    # P6 audit-event (Observability lens) — metadata only, mirrors
    # hook_push_fired's never-the-message-body discipline (payload may carry
    # LLM-authored free text; record what/who/when, not the content).
    ctx.events.emit(
        "hook_event_emitted",
        kind=kind,
        session_id=session_id,
        event_id=event.id,
    )

    return {"kind": "emit_hook_event", "status": "ok", "emitted_kind": kind}


register("emit_hook_event", handle, canonical=emit_hook_event_to_canonical)
