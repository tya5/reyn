"""present kind handler — route bulk data + a display template to the user (FP-0054 PR-A).

**Tier 0** (``ask_user``'s sibling) and **fire-and-continue**: unlike ``ask_user``
this op does NOT pause the run — it produces a presentation ack and returns. The
only gate is that ``data_ref`` read authority resolves identically to
``file.read`` (in ``resolve_present_source``); a denied read raises
``PermissionError`` and the dispatch layer returns ``status="denied"``.

PR-A scope: the declarative model + binding resolution + presentation-guard run
against a **null renderer** — there is no UI surface yet (the inline-CUI renderer
is a later PR). The op returns the compact, high-signal ack
(``{ok, bindings_resolved, bindings_dropped, rows}``, drops as
``{path, reason}``) and emits the ``presented`` P6 event (refs + stats, never
content bytes).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from reyn.core.present import (
    PresentBlueprintError,
    PresentSourceNotFound,
    resolve_bindings,
    resolve_present_source,
    validate_blueprint,
)
from reyn.schemas.models import PresentIROp

from . import register
from .context import OpContext

# The PR-A surface: there is no UI renderer yet, so present renders against a
# null renderer (no bytes reach any surface). Later PRs add "inline-cui" etc.
_NULL_SURFACE = ["null"]

_INLINE_MARKER = "<inline-data>"


def _template_id(op: PresentIROp) -> str:
    """The ``presented`` event's ``template`` field: the registered name, or a
    stable short hash of the inline blueprint (no blueprint bytes in the event)."""
    if op.template is not None:
        return op.template
    digest = hashlib.sha256(
        json.dumps(op.blueprint, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"blueprint:{digest[:16]}"


def _emit_presented(
    ctx: OpContext,
    *,
    data_ref: str,
    template: str,
    ingested: str,
    bindings_resolved: int,
    bindings_dropped: list[dict],
    rows: int,
) -> None:
    """Emit the P6 ``presented`` audit event — refs + stats only, never content
    bytes (the data is already durable in the ref; the event stays light)."""
    ctx.events.emit(
        "presented",
        run_id=ctx.run_id,
        actor=ctx.actor,
        phase=ctx.current_phase,
        data_ref=data_ref,
        template=template,
        surface=list(_NULL_SURFACE),
        ingested=ingested,
        bindings_resolved=bindings_resolved,
        bindings_dropped=bindings_dropped,
        rows=rows,
    )


async def handle(op: PresentIROp, ctx: OpContext) -> dict:
    # 1. Resolve the source under file.read authority (PermissionError propagates
    #    → status="denied"; the read-authority-equivalence invariant).
    if op.data_ref is not None:
        try:
            data, ingested = await resolve_present_source(op.data_ref, ctx)
        except PresentSourceNotFound as exc:
            return {
                "kind": "present", "status": "not_found",
                "ok": False, "error": str(exc),
            }
        data_ref_field: str = op.data_ref
    else:
        # Inline data is in the LLM's context by construction → ingested "full".
        data = op.data_inline
        ingested = "full"
        data_ref_field = _INLINE_MARKER

    template_id = _template_id(op)

    # 2. Named-template resolution (registry + fallback chain) lands in a later
    #    PR; PR-A resolves inline blueprints only. A named template is recorded
    #    (audit-first) but not yet renderable.
    if op.template is not None:
        _emit_presented(
            ctx, data_ref=data_ref_field, template=template_id, ingested=ingested,
            bindings_resolved=0, bindings_dropped=[], rows=0,
        )
        return {
            "kind": "present", "status": "ok", "ok": False,
            "bindings_resolved": 0, "bindings_dropped": [], "rows": 0,
            "note": (
                "named-template resolution is not available yet; author an inline "
                "blueprint (catalog components + $bind path bindings) instead."
            ),
        }

    # 3. Structural gate on the inline blueprint (catalog components + path
    #    bindings only). A malformed blueprint is a hard error, NOT a soft drop.
    try:
        nodes = validate_blueprint(op.blueprint)
    except PresentBlueprintError as exc:
        return {"kind": "present", "status": "error", "ok": False, "error": str(exc)}

    # 4. Bind + guard against the null renderer. The guard applies the surface's
    #    neutralizer strategy (PR-A's null surface uses the terminal strategy).
    resolved = resolve_bindings(nodes, data, surface=_NULL_SURFACE[0])

    # 5. Audit event (P6) — refs + stats, never content bytes.
    _emit_presented(
        ctx,
        data_ref=data_ref_field,
        template=template_id,
        ingested=ingested,
        bindings_resolved=resolved.bindings_resolved,
        bindings_dropped=resolved.bindings_dropped,
        rows=resolved.rows,
    )

    # 6. The compact, high-signal ack (the LLM's only feedback).
    return {
        "kind": "present",
        "status": "ok",
        "ok": True,
        "bindings_resolved": resolved.bindings_resolved,
        "bindings_dropped": resolved.bindings_dropped,
        "rows": resolved.rows,
        "all_bindings_missed": resolved.all_bindings_missed,
    }


register("present", handle)
