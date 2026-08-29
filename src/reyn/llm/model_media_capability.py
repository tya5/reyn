"""Model media-capability query layer (#5509, architect ruling).

Owner instruction (#5509): open reyn's multimodal wire door to litellm's own
content-part vocabulary (document/file/video_url/audio), not just images.
Architect's ruling for how a caller decides WHETHER to embed a given piece
of media inline collapses to one rule, two questions:

  1. Can this model receive these bytes at all?
  2. Can the cost be bounded before sending?

Either "No" routes to the lossless path-ref degrade stage instead of inline
embedding — a ref is a degrade, never a silent drop (owner: "捨てるのはバグ").
This module answers question 1 only; question 2 (a genuine per-item token
bound) is a property of the MEDIA TYPE, not the model, and is decided by the
caller (``reyn.runtime.router_loop``) — today only images have one (a fixed
per-frame estimate, single-sourced from the compaction engine's own
``_IMAGE_FIXED_TOKEN_COST``); a PDF's cost scales with page count, so no
single constant upper-bounds it (a constant here would be a wish, not a
bound) — see that module's own comments for the full reasoning.

Three states, never collapsed to two (the ``None``-means-two-things shape
architect rejected in #5475's ``SeqUnavailable`` ruling, same family here):

  SUPPORTED   — litellm's catalog (or an operator override) says yes.
  UNSUPPORTED — litellm's catalog (or an operator override) says no.
  UNKNOWN     — the model isn't in litellm's catalog, litellm hasn't
                finished importing yet in this process, or the catalog entry
                doesn't carry an opinion for this capability field (litellm
                itself returns ``None`` for a field it hasn't measured).

UNKNOWN and UNSUPPORTED both route to the ref stage — architect: "unknown を
「可能」と読まない。推測で turn を賭けない" (never guess a capability from
absence; the cost of guessing wrong is losing a whole turn to a provider
error). The escape hatch for a model litellm's catalog genuinely doesn't
know (a custom proxy alias, a self-hosted deployment) is
:func:`register_media_capability_overrides` — a DECLARATION, not a guess,
mirroring ``model_budget.py``'s own ``register_max_input_overrides`` for
exactly the same class of gap (litellm's catalog not knowing an operator's
own model string).

Mirrors ``model_budget.py``'s design throughout (this module intentionally
duplicates that module's shape rather than sharing code with it — the two
answer different questions over the same catalog and have already diverged
once, #4680②'s NOT_READY/UNCATALOGED split, in a way specific to
``max_input_tokens``'s own fallback-VALUE semantics that don't apply here):
the non-blocking ``ensure_litellm_ready_or_defer`` chokepoint (never blocks
the calling thread on litellm's own background warm-up), the ``#1162``
provider-prefix-strip retry (a proxied model missing from the catalog under
its prefix but present under the bare name), and the conflict-detecting
override registry (a second, DIFFERENT declaration for the same model
string raises rather than silently overwriting an earlier session's).
"""
from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger(__name__)


class MediaCapability(str, Enum):
    """Three states — see module docstring for why never two."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class MediaCapabilityConflictError(ValueError):
    """Two DIFFERENT registrations (same process, possibly different
    sessions/configs) declared a different capability value for the SAME
    (model, capability_field) pair. Raised rather than silently letting the
    later registration win — mirrors ``model_budget.MaxInputTokensConflictError``."""


# Operator-declared overrides: {model: {capability_field: bool}}. PROCESS-
# SHARED, same lifetime/scope as model_budget.py's own override registry —
# reyn can hold multiple sessions in one process.
_config_capability_overrides: "dict[str, dict[str, bool]]" = {}

# lead-coder finding (#5509, real measurement): get_model_info RAISES for
# any model string it doesn't recognize — and the owner's own standing
# deployment goes through a proxy (API-key handling is the proxy's job, a
# permanent instruction), so "openai/my-proxy-model"-shaped strings miss
# litellm's static catalog AS A MATTER OF COURSE, not as an edge case.
# Meaning UNKNOWN is not a rare fallback here — for a proxied deployment it
# is the ORDINARY case, and every non-text attachment would silently
# degrade to ref forever unless the operator finds and uses
# register_media_capability_overrides. "Silently" is the actual defect:
# from the user's side this reads as "images stopped working", with
# nothing pointing at the fix. Warn ONCE per (model, capability_field) the
# first time this module resolves UNKNOWN via a genuine catalog miss (not
# litellm-still-warming, which self-corrects and would be pure noise to
# warn about every process start) — same "warn once, keyed" shape
# model_budget.py's own _warned_models already uses.
_warned_uncataloged: "set[tuple[str, str]]" = set()


def _warn_uncataloged_once(model: str, capability_field: str) -> None:
    key = (model, capability_field)
    if key in _warned_uncataloged:
        return
    _warned_uncataloged.add(key)
    logger.warning(
        "media_capability_unknown: litellm has no catalog entry for "
        "model=%r (capability=%r) — every non-text attachment for this "
        "model will degrade to a lossless path-ref instead of being sent "
        "inline, until you declare its real capability. Fix: add "
        "`multimodal.model_capability_overrides.%s.%s: true` (or `false`) "
        "to reyn.yaml.",
        model, capability_field, model, capability_field,
    )


def register_media_capability_overrides(mapping: "dict[str, dict[str, bool]]") -> None:
    """Register *mapping* (``{model: {capability_field: bool}}``, from
    ``MultimodalConfig.model_capability_overrides``) into the process-shared
    registry :func:`get_media_capability` consults first, ahead of litellm's
    catalog.

    Idempotent for an identical re-registration; a genuine conflict (same
    model + capability_field, a DIFFERENT value) raises
    :class:`MediaCapabilityConflictError`. Call once per ``Session``
    construction — never inside a hot path."""
    for model, fields in mapping.items():
        existing = _config_capability_overrides.setdefault(model, {})
        for capability_field, value in fields.items():
            prior = existing.get(capability_field)
            if prior is not None and prior != value:
                raise MediaCapabilityConflictError(
                    f"conflicting {capability_field!r} registered for model "
                    f"{model!r}: {prior} (already registered) vs {value} "
                    f"(new registration) — two different sessions/configs in "
                    f"this process declared different values for the same "
                    f"model + capability. Give them the same value, or route "
                    f"them through different model strings."
                )
            existing[capability_field] = value


def _catalog_capability(model: str, capability_field: str) -> "tuple[MediaCapability, bool]":
    """Query litellm's catalog for *capability_field* on *model*, with no
    operator-override consultation (that happens one level up, in
    :func:`get_media_capability`) — mirrors ``model_budget._lookup_max_
    input``'s split between pure-catalog-lookup and full resolution.

    Returns ``(capability, transient)``. ``transient=True`` only for the
    "litellm hasn't finished importing yet" case — self-corrects on a
    later call, so it must never trigger the uncataloged-model warning
    (that would fire on every cold process start, pure noise). A genuine
    catalog miss (litellm loaded, model or field just isn't in it) is
    ``transient=False`` — this IS the ordinary case for a proxied
    deployment (see this module's own ``_warned_uncataloged`` comment)."""
    from reyn.llm.litellm_bootstrap import (
        LitellmWarmingInBackgroundError,
        ensure_litellm_ready_or_defer,
    )

    try:
        litellm = ensure_litellm_ready_or_defer()
    except LitellmWarmingInBackgroundError:
        # litellm has not finished importing in THIS process yet — the
        # NOT_READY half of #4680②'s split, collapsed into UNKNOWN here:
        # unlike max_input_tokens (which has a safe numeric fallback to
        # degrade to), a media-embed decision has no safe numeric middle
        # ground — "unknown" already IS the correct, safe answer for both
        # NOT_READY and a genuine catalog miss (route to ref either way).
        return MediaCapability.UNKNOWN, True
    try:
        info = litellm.get_model_info(model)
    except Exception:
        return MediaCapability.UNKNOWN, False
    value = info.get(capability_field)
    if value is True:
        return MediaCapability.SUPPORTED, False
    if value is False:
        return MediaCapability.UNSUPPORTED, False
    # litellm itself carries no opinion for this field on this (known)
    # model — its own `None`, not ours; still UNKNOWN, never guessed.
    return MediaCapability.UNKNOWN, False


def get_media_capability(model: str, capability_field: str) -> MediaCapability:
    """Return whether *model* supports *capability_field* (a litellm
    ``get_model_info`` boolean field name — e.g. ``"supports_vision"``,
    ``"supports_pdf_input"``, ``"supports_audio_input"``).

    Resolution order (operator override wins unconditionally, mirrors
    ``model_budget``'s own ``#4689`` priority — "catalog が外れた時だけ、
    ではない"):
      1. :func:`register_media_capability_overrides`-declared value.
      2. litellm's catalog, under *model* as given.
      3. ``#1162``: if *model* is provider-prefixed (``openai/gemini-...``),
         retry under the bare suffix — a proxied model can miss the catalog
         under its prefix but resolve under the bare name.
      4. :attr:`MediaCapability.UNKNOWN` — never guessed as SUPPORTED. A
         genuine (non-transient) catalog miss also warns once per (model,
         capability_field) — see ``_warn_uncataloged_once``.
    """
    override = _config_capability_overrides.get(model, {}).get(capability_field)
    if override is not None:
        return MediaCapability.SUPPORTED if override else MediaCapability.UNSUPPORTED

    result, transient = _catalog_capability(model, capability_field)
    if result is not MediaCapability.UNKNOWN:
        return result

    if "/" in model:
        bare = model.split("/", 1)[1]
        bare_result, bare_transient = _catalog_capability(bare, capability_field)
        if bare_result is not MediaCapability.UNKNOWN:
            return bare_result
        transient = transient or bare_transient

    if not transient:
        _warn_uncataloged_once(model, capability_field)
    return MediaCapability.UNKNOWN
