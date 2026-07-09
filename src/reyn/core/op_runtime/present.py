"""present kind handler — route bulk data + a declarative view to the user
(FP-0054 PR-A/B/C; ``view`` rename + optional view/blueprint: FP-0055 PR-1).

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

**PR-C — named views + the §3 4-stage fallback chain.** ``op.view`` is a
registered name resolved against ``OpContext.presentation_registry`` (the operator's
``presentations.yaml`` — a named view's value is a validated blueprint, the same
downstream ``resolve_bindings`` → render path as an inline blueprint). The view
source falls back through 4 stages (never a hard failure — the data always reaches
the user): **(1)** a resolvable registered ``view`` → **(2)** an inline
``blueprint`` → **(3)** a content-type default viewer synthesized from the data's
shape → **(4)** a generic YAML/text dump of the whole value. The fallback fires when
the requested rendering cannot produce a usable presentation — an UNKNOWN view
name, or a view whose bindings ALL miss the data (``all_bindings_missed`` — don't
show an empty shell). The ack + ``presented`` event carry the REQUESTED rendering's
stats (so the LLM's self-correction loop still sees its view all-missed), plus a
``note`` naming the fallback viewer that actually reached the user. A malformed INLINE
blueprint stays a HARD error (``status="error"``) — that is a view bug, not a
fallback trigger.

**FP-0055 PR-1 — optional view/blueprint (``mode: "default"``).** When NEITHER
``view`` nor ``blueprint`` is supplied, the caller-requested stage (1/2) is skipped
entirely and resolution enters directly at stage 3 (the content-type default
viewer) — a one-shot ``present(data_ref=...)`` "just shows" the data. This is the
INTENDED rendering, not a fallback: the ack carries the default viewer's own stats
and NO ``note`` unless stage 3 itself degrades further to stage 4 (still nothing to
"fall back from" — there was no requested view). The ack's ``mode`` field
(``view`` | ``blueprint`` | ``default``) discriminates which of the three inputs
the caller gave.
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

# The ack/event ``mode`` discriminator (FP-0055 PR-1) — which of the three
# mutually-exclusive inputs the caller gave, independent of which stage
# actually rendered.
_MODE_VIEW = "view"
_MODE_BLUEPRINT = "blueprint"
_MODE_DEFAULT = "default"


def _mode(op: PresentIROp) -> str:
    """The caller-input discriminator: ``view`` | ``blueprint`` | ``default``
    (neither given — routes straight to the stage-3/4 default viewer)."""
    if op.view is not None:
        return _MODE_VIEW
    if op.blueprint is not None:
        return _MODE_BLUEPRINT
    return _MODE_DEFAULT


def _view_id(op: PresentIROp) -> "str | None":
    """The ``presented`` event's ``view`` field: the registered name, a stable
    short hash of the inline blueprint (no blueprint bytes in the event), or
    ``None`` when neither was given (the ``mode: "default"`` case)."""
    if op.view is not None:
        return op.view
    if op.blueprint is not None:
        digest = hashlib.sha256(
            json.dumps(op.blueprint, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return f"blueprint:{digest[:16]}"
    return None


def _surface_name(ctx: OpContext) -> str:
    """The surface a resolved presentation reaches: the wired renderer's own name, or
    the PR-A null surface when none is wired."""
    renderer = ctx.presentation_renderer
    return renderer.surface_name if renderer is not None else _NULL_SURFACE


def _emit_presented(
    ctx: OpContext,
    *,
    data_ref: str,
    view: "str | None",
    mode: str,
    surface: str,
    ingested: str,
    bindings_resolved: int,
    bindings_dropped: list[dict],
    rows: int,
) -> None:
    """Emit the P6 ``presented`` audit event — refs + stats only, never content
    bytes (the data is already durable in the ref; the event stays light).

    ``view`` (FP-0055 PR-1 rename of the former ``template`` field): the
    registered name, ``blueprint:<hash>`` for an inline blueprint, or ``None``
    when neither was given (``mode: "default"``)."""
    ctx.events.emit(
        "presented",
        run_id=ctx.run_id,
        actor=ctx.actor,
        phase=ctx.current_phase,
        data_ref=data_ref,
        view=view,
        mode=mode,
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

    mode = _mode(op)
    view_id = _view_id(op)
    surface = _surface_name(ctx)

    # 2. Resolve the presentation through the §3 4-stage fallback chain. A
    #    malformed INLINE blueprint is a HARD error (a view bug), NOT a
    #    fallback trigger — unknown view names / all-miss views fall
    #    through instead. Omitting both view and blueprint (``mode="default"``)
    #    enters directly at stage 3 — that IS the requested rendering.
    try:
        requested, rendered, fallback_stage = _resolve_presentation(
            op, data, mode=mode, surface=surface, registry=ctx.presentation_registry,
        )
    except PresentBlueprintError as exc:
        return {"kind": "present", "status": "error", "ok": False, "error": str(exc)}

    # 3. Audit event (P6) — refs + stats, never content bytes. Non-default modes
    #    audit the REQUESTED rendering's stats (a deterministic display detail is
    #    derivable from the recorded data_ref); the default mode's requested
    #    rendering IS the synthesized default viewer, so it audits ``rendered``.
    stats = _rendered_stats(rendered) if mode == _MODE_DEFAULT else _requested_stats(requested)
    _emit_presented(
        ctx,
        data_ref=data_ref_field,
        view=view_id,
        mode=mode,
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
    #    ``note`` carry the "your view did not match" self-correction signal
    #    when a fallback fired. ``mode`` discriminates which of the three inputs
    #    the caller gave (FP-0055 PR-1); for ``mode: "default"`` the stats above
    #    are already the default viewer's own — no fallback happened, so no note
    #    unless stage 3 itself degraded further to stage 4.
    ack = {
        "kind": "present",
        "status": "ok",
        "ok": True,
        "mode": mode,
        "bindings_resolved": stats["bindings_resolved"],
        "bindings_dropped": stats["bindings_dropped"],
        "rows": stats["rows"],
        "all_bindings_missed": stats["all_bindings_missed"],
    }
    note = _fallback_note(op, mode, requested, fallback_stage)
    if note is not None:
        ack["note"] = note
    return ack


def _resolve_presentation(
    op: PresentIROp, data: Any, *, mode: str, surface: str, registry: Any,
) -> "tuple[ResolvedPresentation | None, ResolvedPresentation, str | None]":
    """Run the FP-0054 §3 4-stage view-source fallback chain (FP-0055 PR-1:
    ``mode="default"`` skips straight to stage 3).

    Returns ``(requested, rendered, fallback_stage)``:

    - ``requested`` — the ``ResolvedPresentation`` of the caller-requested rendering
      (stage 1 registered ``view`` or stage 2 inline ``blueprint``), or ``None``
      when the requested view name is UNKNOWN (nothing was bound) or when
      ``mode="default"`` (no stage 1/2 was attempted at all). The ack + event
      report these stats (non-default modes) so the LLM's self-correction loop
      sees its own view's outcome.
    - ``rendered`` — the ``ResolvedPresentation`` actually handed to the surface: the
      requested one when it produced a usable presentation (≥1 binding resolved, or a
      literal-only view), else a synthesized fallback — stage 3 (content-type
      default viewer), then stage 4 (generic YAML/text, which always renders).
    - ``fallback_stage`` — ``None`` when the requested rendering was used directly,
      OR when ``mode="default"`` and stage 3 rendered successfully (the default
      viewer is itself the requested rendering here, not a fallback from
      anything); else the fallback stage that rendered.

    A malformed INLINE blueprint raises ``PresentBlueprintError`` (a hard error, not
    a fallback trigger) — the caller surfaces ``status="error"``. A registered
    view is already validated at registry-build time, so it never re-validates
    here.
    """
    requested: "ResolvedPresentation | None" = None
    if mode == _MODE_VIEW:
        nodes = registry.get(op.view) if registry is not None else None
        if nodes is not None:
            requested = resolve_bindings(nodes, data, surface=surface)
    elif mode == _MODE_BLUEPRINT:
        # Inline blueprint — the structural gate (hard error preserved), then bind.
        nodes = validate_blueprint(op.blueprint)
        requested = resolve_bindings(nodes, data, surface=surface)
    # else mode == _MODE_DEFAULT: neither given — requested stays None, no
    # stage 1/2 attempted; resolution enters directly at stage 3 below.

    # Requested rendering is usable → no fallback. ``all_bindings_missed`` is True
    # only when the view had ≥1 binding and none resolved (a literal-only or a
    # partially-hitting view is usable and renders as-is).
    if mode != _MODE_DEFAULT and requested is not None and not requested.all_bindings_missed:
        return requested, requested, None

    # Stage 3 — content-type default viewer (synthesized from the data's shape).
    stage3 = resolve_bindings(
        validate_blueprint(default_viewer_blueprint(data)), data, surface=surface,
    )
    if not stage3.all_bindings_missed:
        # In default mode, stage 3 IS the requested rendering — not a fallback.
        fallback_stage = None if mode == _MODE_DEFAULT else _STAGE_CONTENT_TYPE
        return requested, stage3, fallback_stage

    # Stage 4 — generic YAML/text (always renders — the final catch).
    stage4 = resolve_bindings(
        validate_blueprint(generic_blueprint(data)), data, surface=surface,
    )
    return requested, stage4, _STAGE_GENERIC


def _rendered_stats(rendered: "ResolvedPresentation") -> dict:
    """The ack + event binding-stats read directly off a resolved rendering
    (used for ``mode: "default"``, where the synthesized default/generic viewer
    IS the requested rendering — see :func:`_resolve_presentation`)."""
    return {
        "bindings_resolved": rendered.bindings_resolved,
        "bindings_dropped": rendered.bindings_dropped,
        "rows": rendered.rows,
        "all_bindings_missed": rendered.all_bindings_missed,
    }


def _requested_stats(requested: "ResolvedPresentation | None") -> dict:
    """The ack + event binding-stats for the caller-requested rendering. An unknown
    view name (``requested is None``) reports zeros — the LLM asked for a
    view that resolved nothing; the ``note`` explains the fallback."""
    if requested is None:
        return {
            "bindings_resolved": 0, "bindings_dropped": [], "rows": 0,
            "all_bindings_missed": False,
        }
    return _rendered_stats(requested)


def _fallback_note(
    op: PresentIROp,
    mode: str,
    requested: "ResolvedPresentation | None",
    fallback_stage: "str | None",
) -> "str | None":
    """The ack ``note`` naming the fallback viewer that reached the user, or ``None``
    when the requested rendering was used directly (including ``mode: "default"``
    rendering successfully at stage 3 — that is the intended behavior, not a
    fallback)."""
    if fallback_stage is None:
        return None
    viewer = (
        "content-type default viewer"
        if fallback_stage == _STAGE_CONTENT_TYPE
        else "generic YAML/text viewer"
    )
    if mode == _MODE_DEFAULT:
        # Stage 3 (the default viewer) itself degraded further to stage 4 —
        # there is no requested view to "fall back from".
        return (
            "no view or blueprint given and the default viewer's bindings also "
            f"missed — presented via the {viewer} so the data still reached the "
            "user."
        )
    if mode == _MODE_VIEW and requested is None:
        return (
            f"view {op.view!r} is not registered — presented via the "
            f"{viewer} so the data still reached the user."
        )
    return (
        f"all bindings missed — presented via the {viewer} so the data still "
        "reached the user (re-check the view against the data shape)."
    )


from reyn.core.offload.canonical import STRUCTURED_PASSTHROUGH  # noqa: E402

register("present", handle, canonical=STRUCTURED_PASSTHROUGH)
