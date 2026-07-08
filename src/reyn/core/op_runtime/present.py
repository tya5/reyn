"""present kind handler — route bulk data + a display template to the user (FP-0054 PR-A/B/C).

**Tier 0** (``ask_user``'s sibling) and **fire-and-continue**: unlike ``ask_user``
this op does NOT pause the run — it produces a presentation ack and returns. The
only gate is that ``data_ref`` read authority resolves identically to
``file.read`` (in ``resolve_present_source``); a denied read raises
``PermissionError`` and the dispatch layer returns ``status="denied"``.

The surface is whichever ``OpContext.presentation_renderer`` the caller wired (PR-B: the
inline-CUI's ``OutboxPresentationRenderer``, ``runtime/session_buses.py``) — ``None`` keeps
PR-A's null-surface behavior (no UI reached, ``surface="null"``). The op returns the
compact, high-signal ack (``{ok, bindings_resolved, bindings_dropped, rows}``, drops as
``{path, reason}``) and emits the ``presented`` P6 event (refs + stats, never content
bytes) regardless of whether a renderer is wired.

**PR-C — named templates + the §3 4-stage fallback chain.** ``op.template`` is a
registered name resolved against ``OpContext.presentation_registry`` (the operator's
``presentations.yaml`` — a named template's value is a validated blueprint, the same
downstream ``resolve_bindings`` → render path as an inline blueprint). The template
source falls back through 4 stages (never a hard failure — the data always reaches
the user): **(1)** a resolvable registered ``template`` → **(2)** an inline
``blueprint`` → **(3)** a content-type default viewer synthesized from the data's
shape → **(4)** a generic YAML/text dump of the whole value. The fallback fires when
the requested rendering cannot produce a usable presentation — an UNKNOWN template
name, or a template whose bindings ALL miss the data (``all_bindings_missed`` — don't
show an empty shell). The ack + ``presented`` event carry the REQUESTED rendering's
stats (so the LLM's self-correction loop still sees its template all-missed), plus a
``note`` naming the fallback viewer that actually reached the user. A malformed INLINE
blueprint stays a HARD error (``status="error"``) — that is a template bug, not a
fallback trigger.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from reyn.core.present import (
    PresentBlueprintError,
    PresentSourceNotFound,
    ResolvedPresentation,
    default_viewer_blueprint,
    generic_blueprint,
    resolve_bindings,
    resolve_present_source,
    validate_blueprint,
)
from reyn.schemas.models import PresentIROp

from . import register
from .context import OpContext

_NULL_SURFACE = "null"

_INLINE_MARKER = "<inline-data>"

# The §3 fallback-stage labels used in the ack ``note`` (never in the fixed
# ``presented`` event shape).
_STAGE_CONTENT_TYPE = "content_type_default"
_STAGE_GENERIC = "generic"


def _template_id(op: PresentIROp) -> str:
    """The ``presented`` event's ``template`` field: the registered name, or a
    stable short hash of the inline blueprint (no blueprint bytes in the event)."""
    if op.template is not None:
        return op.template
    digest = hashlib.sha256(
        json.dumps(op.blueprint, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"blueprint:{digest[:16]}"


def _surface_name(ctx: OpContext) -> str:
    """The surface a resolved presentation reaches: the wired renderer's own name, or
    the PR-A null surface when none is wired."""
    renderer = ctx.presentation_renderer
    return renderer.surface_name if renderer is not None else _NULL_SURFACE


def _emit_presented(
    ctx: OpContext,
    *,
    data_ref: str,
    template: str,
    surface: str,
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
        surface=[surface],
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
    surface = _surface_name(ctx)

    # 2. Resolve the presentation through the §3 4-stage fallback chain. A
    #    malformed INLINE blueprint is a HARD error (a template bug), NOT a
    #    fallback trigger — unknown template names / all-miss templates fall
    #    through instead.
    try:
        requested, rendered, fallback_stage = _resolve_presentation(
            op, data, surface=surface, registry=ctx.presentation_registry,
        )
    except PresentBlueprintError as exc:
        return {"kind": "present", "status": "error", "ok": False, "error": str(exc)}

    # 3. Audit event (P6) — refs + REQUESTED-rendering stats, never content bytes.
    #    (The fallback rendering is a deterministic display detail derivable from
    #    the recorded data_ref; the event audits what the caller requested.)
    stats = _requested_stats(requested)
    _emit_presented(
        ctx,
        data_ref=data_ref_field,
        template=template_id,
        surface=surface,
        ingested=ingested,
        bindings_resolved=stats["bindings_resolved"],
        bindings_dropped=stats["bindings_dropped"],
        rows=stats["rows"],
    )

    # 4. Hand the actually-rendered model to the wired surface (PR-B). Fire-and-
    #    continue: the op's ack (below) is already fully derived from the stats,
    #    not from anything the renderer does — a renderer failure must never be
    #    allowed to reach here (see OutboxPresentationRenderer's own fire-and-forget
    #    contract).
    if ctx.presentation_renderer is not None:
        ctx.presentation_renderer.render(rendered)

    # 5. The compact, high-signal ack (the LLM's only feedback). ``ok`` is True
    #    because SOME presentation reached the user; ``all_bindings_missed`` +
    #    ``note`` carry the "your template did not match" self-correction signal
    #    when a fallback fired.
    ack = {
        "kind": "present",
        "status": "ok",
        "ok": True,
        "bindings_resolved": stats["bindings_resolved"],
        "bindings_dropped": stats["bindings_dropped"],
        "rows": stats["rows"],
        "all_bindings_missed": stats["all_bindings_missed"],
    }
    note = _fallback_note(op, requested, fallback_stage)
    if note is not None:
        ack["note"] = note
    return ack


def _resolve_presentation(
    op: PresentIROp, data: Any, *, surface: str, registry: Any,
) -> "tuple[ResolvedPresentation | None, ResolvedPresentation, str | None]":
    """Run the FP-0054 §3 4-stage template-source fallback chain.

    Returns ``(requested, rendered, fallback_stage)``:

    - ``requested`` — the ``ResolvedPresentation`` of the caller-requested rendering
      (stage 1 registered ``template`` or stage 2 inline ``blueprint``), or ``None``
      when the requested template name is UNKNOWN (nothing was bound). The ack +
      event report these stats so the LLM's self-correction loop sees its own
      template's outcome.
    - ``rendered`` — the ``ResolvedPresentation`` actually handed to the surface: the
      requested one when it produced a usable presentation (≥1 binding resolved, or a
      literal-only template), else a synthesized fallback — stage 3 (content-type
      default viewer), then stage 4 (generic YAML/text, which always renders).
    - ``fallback_stage`` — ``None`` when the requested rendering was used, else the
      fallback stage that rendered.

    A malformed INLINE blueprint raises ``PresentBlueprintError`` (a hard error, not
    a fallback trigger) — the caller surfaces ``status="error"``. A registered
    template is already validated at registry-build time, so it never re-validates
    here.
    """
    requested: "ResolvedPresentation | None" = None
    if op.template is not None:
        nodes = registry.get(op.template) if registry is not None else None
        if nodes is not None:
            requested = resolve_bindings(nodes, data, surface=surface)
    else:
        # Inline blueprint — the structural gate (hard error preserved), then bind.
        nodes = validate_blueprint(op.blueprint)
        requested = resolve_bindings(nodes, data, surface=surface)

    # Requested rendering is usable → no fallback. ``all_bindings_missed`` is True
    # only when the template had ≥1 binding and none resolved (a literal-only or a
    # partially-hitting template is usable and renders as-is).
    if requested is not None and not requested.all_bindings_missed:
        return requested, requested, None

    # Stage 3 — content-type default viewer (synthesized from the data's shape).
    stage3 = resolve_bindings(
        validate_blueprint(default_viewer_blueprint(data)), data, surface=surface,
    )
    if not stage3.all_bindings_missed:
        return requested, stage3, _STAGE_CONTENT_TYPE

    # Stage 4 — generic YAML/text (always renders — the final catch).
    stage4 = resolve_bindings(
        validate_blueprint(generic_blueprint(data)), data, surface=surface,
    )
    return requested, stage4, _STAGE_GENERIC


def _requested_stats(requested: "ResolvedPresentation | None") -> dict:
    """The ack + event binding-stats for the caller-requested rendering. An unknown
    template name (``requested is None``) reports zeros — the LLM asked for a
    template that resolved nothing; the ``note`` explains the fallback."""
    if requested is None:
        return {
            "bindings_resolved": 0, "bindings_dropped": [], "rows": 0,
            "all_bindings_missed": False,
        }
    return {
        "bindings_resolved": requested.bindings_resolved,
        "bindings_dropped": requested.bindings_dropped,
        "rows": requested.rows,
        "all_bindings_missed": requested.all_bindings_missed,
    }


def _fallback_note(
    op: PresentIROp, requested: "ResolvedPresentation | None", fallback_stage: "str | None",
) -> "str | None":
    """The ack ``note`` naming the fallback viewer that reached the user, or ``None``
    when the requested rendering was used directly."""
    if fallback_stage is None:
        return None
    viewer = (
        "content-type default viewer"
        if fallback_stage == _STAGE_CONTENT_TYPE
        else "generic YAML/text viewer"
    )
    if op.template is not None and requested is None:
        return (
            f"template {op.template!r} is not registered — presented via the "
            f"{viewer} so the data still reached the user."
        )
    return (
        f"all bindings missed — presented via the {viewer} so the data still "
        "reached the user (re-check the template against the data shape)."
    )


register("present", handle)
