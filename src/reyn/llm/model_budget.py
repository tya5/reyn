"""Model-budget query layer.

Provides `get_max_input_tokens(model)` which wraps LiteLLM's model catalog
query. The function is the single source of truth for "how large is this
model's context window?" inside the OS — callers should not duplicate the
LiteLLM call.

Fallback policy (unknown models):
    When LiteLLM does not have an entry for the given model string, a
    conservative default of 128_000 tokens is returned and a one-time
    ``model_budget_fallback`` observability event is emitted so the operator
    knows the model is not cataloged. 128_000 was chosen as a floor that is
    below all commercial production models' actual context windows, so the
    compaction logic errs on the side of compacting more rather than less.

#4680 ②: the fallback has TWO distinct causes, previously conflated into the
same value AND the same message — :class:`MaxInputTokensFallbackReason`.
``NOT_READY`` (litellm has not finished importing in this process yet — a
TEMPORARY state; a later call, once litellm is ready, resolves correctly)
vs ``UNCATALOGED`` (litellm IS loaded but has no entry, or no positive
``max_input_tokens``, for this exact model string — a PERMANENT state for
this process; it will not resolve differently on a later call unless the
operator declares an override or litellm's own catalog is updated). The
128_000 fallback VALUE and the "warn once per model" policy are unchanged
by this split (out of scope — see #4680's own PR, ``register_max_input_
overrides`` already exists for the "declare it yourself" escape hatch);
only the OBSERVATION of which state produced it is split, at the two
places an operator can actually see it: the log/event message text
(``_resolve_max_input``'s ``source`` string, this module's warning) and
``get_max_input_tokens_source``'s return value, which the context-budget
advisor's status-bar chip (``context_budget_advisor.py``'s
``_effective_trigger_source``/``raw_context_window``) already surfaces —
a NOT_READY case reaching the status bar previously read identically to a
genuinely-uncataloged model, with no way for the operator to tell "this
will fix itself" from "this needs a config declaration". A NOT_READY
warning is also, uniquely, CORRECTED once litellm becomes ready and the
model resolves (or is found genuinely uncataloged) — "warned once, never
corrected" is right for the permanent UNCATALOGED case but wrong for the
temporary NOT_READY one (lead-coder review, #4680②).

Priority order (#4689, owner instruction): operator-declared config >
LiteLLM catalog > the 128K fallback above — unconditionally, not just when
the catalog lookup fails. ``register_max_input_overrides`` is how a
``llm.models.<tier>.max_input_tokens`` declaration reaches this module:
every one of this file's 8 call sites only ever holds an already-resolved
LiteLLM model STRING (never a class name or a ``ModelResolver``), so the
override is registered ONCE, wherever a ``ModelResolver`` is actually
constructed (``reyn.llm.model_resolver.ModelResolver.max_input_token_overrides``
does the class-name -> model-string resolution), keyed by model string —
every call site here benefits without any of them changing.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.core.events.events import EventLog

logger = logging.getLogger(__name__)

# Conservative default when LiteLLM does not recognize the model.
# 128K is a reasonable floor — all modern production models (Gemini, GPT-4o,
# Claude 3.x) have context windows of ≥128K, so compaction using this default
# will trigger earlier than necessary but never allow the prompt to exceed the
# real budget.
_FALLBACK_MAX_INPUT_TOKENS = 128_000


class MaxInputTokensFallbackReason(str, Enum):
    """#4680 ②: why ``get_max_input_tokens`` fell back to the conservative
    default — see this module's own docstring for the full rationale for
    why these two states were worth distinguishing at all."""

    #: litellm has not finished importing in this process yet
    #: (``ensure_litellm_ready_or_defer`` raised
    #: ``LitellmWarmingInBackgroundError``) — TEMPORARY, self-corrects.
    NOT_READY = "not_ready"
    #: litellm IS loaded, but has no catalog entry (or no positive
    #: ``max_input_tokens``) for this exact model string — PERMANENT for
    #: this process.
    UNCATALOGED = "uncataloged"


# Tracks the LAST reason a model was warned/logged for, so a later call
# that resolves the SAME model differently (litellm finished loading, or a
# genuinely-uncataloged verdict is now available) can emit a correction —
# #4680②'s own "a warning correction" requirement. `None` (absent from the
# dict) means "never warned" — the same meaning membership in the old
# `set[str]` this replaces carried. Keyed by model string, same scope/
# lifetime as before (process-shared).
_warned_models: "dict[str, MaxInputTokensFallbackReason]" = {}

# #4689: operator-declared max_input_tokens, keyed by resolved LiteLLM
# model string. PROCESS-SHARED, same lifetime/scope as _warned_models
# above — reyn can hold multiple sessions (possibly different projects,
# different configs) in one process. Cumulative collision detection (see
# register_max_input_overrides) is what keeps that sharing safe: a second,
# DIFFERENT declaration for the same model string is rejected loudly
# rather than silently overwriting the first session's value (lead-coder
# ruling, #4689 — the same "config wins here, silently doesn't win there"
# confusion #4680 already caused, this time from cross-session sharing
# rather than cross-call-site sharing).
_config_max_input_overrides: "dict[str, int]" = {}


class MaxInputTokensConflictError(ValueError):
    """Two DIFFERENT registrations (same process, possibly different
    sessions/configs) declared a different max_input_tokens for the SAME
    resolved model string. Raised rather than silently letting the later
    registration win — an operator whose config IS being honored must
    never end up unknowingly overridden by another session's config for
    the same model."""


def register_max_input_overrides(mapping: "dict[str, int]") -> None:
    """Register *mapping* (``{model string: max_input_tokens}``, typically
    ``ModelResolver.max_input_token_overrides()``'s own return value) into
    the process-shared registry ``_resolve_max_input`` consults first,
    ahead of the LiteLLM catalog.

    Idempotent for an identical re-registration (the same model string,
    the same value — e.g. two Sessions sharing one project's config) —
    only a GENUINE conflict (same model string, a DIFFERENT value) raises
    :class:`MaxInputTokensConflictError`. Call once per ``ModelResolver``
    construction (``registry_bootstrap.py`` and the 3 other sites that
    build one) — never inside a hot path, this is a startup-time
    registration, not a per-call operation."""
    for model, value in mapping.items():
        existing = _config_max_input_overrides.get(model)
        if existing is not None and existing != value:
            raise MaxInputTokensConflictError(
                f"conflicting max_input_tokens registered for model "
                f"{model!r}: {existing} (already registered) vs {value} "
                f"(new registration) — two different sessions/configs in "
                f"this process declared different values for the same "
                f"model string. Give them the same value, or route them "
                f"through different model classes/strings."
            )
        _config_max_input_overrides[model] = value


def _lookup_max_input(model: str) -> "tuple[int | None, MaxInputTokensFallbackReason | None]":
    """Return (``max_input_tokens``, reason) from LiteLLM's catalog for
    *model*. ``(value, None)`` on success; ``(None, reason)`` on failure —
    ``reason`` is :attr:`MaxInputTokensFallbackReason.NOT_READY` when
    litellm itself hasn't finished importing yet (distinguishable from a
    genuine catalog miss, #4680②), else
    :attr:`MaxInputTokensFallbackReason.UNCATALOGED`.

    No fallback VALUE, no events — pure catalog lookup so callers can
    compose retries (e.g. provider-prefix-strip, #1162).
    """
    try:
        # #4395 PR-2: non-blocking chokepoint variant — this function
        # already has a safe fallback (128K conservative default, in
        # `_resolve_max_input` below) for "no answer yet", so there is no
        # reason to wait for litellm here at all, let alone on the calling
        # thread. `ensure_litellm_ready_or_defer()` never imports litellm on
        # this thread if it isn't already warm — it kicks off the one
        # dedicated background thread instead (litellm_bootstrap.py's own
        # PR-2 section) and raises immediately, reaching the `except` below
        # with no wait paid.
        from reyn.llm.litellm_bootstrap import (
            LitellmWarmingInBackgroundError,
            ensure_litellm_ready_or_defer,
        )
        try:
            litellm = ensure_litellm_ready_or_defer()
        except LitellmWarmingInBackgroundError:
            # #4680②: litellm has not finished importing in THIS process
            # yet — distinct from, and caught BEFORE, the broader
            # `except Exception` below, which is reached only once
            # litellm has actually been obtained (a genuine catalog miss).
            return None, MaxInputTokensFallbackReason.NOT_READY
        info = litellm.get_model_info(model)
        max_input = info.get("max_input_tokens")
        if max_input and int(max_input) > 0:
            return int(max_input), None
    except Exception:
        pass  # Not in catalog / no positive window — litellm WAS obtained.
    return None, MaxInputTokensFallbackReason.UNCATALOGED


def _resolve_max_input(
    model: str,
) -> "tuple[int, str, MaxInputTokensFallbackReason | None]":
    """Resolve (value, source, fallback_reason) exactly once — the single
    place ``get_max_input_tokens`` and ``get_max_input_tokens_source``
    both delegate to, so the #1162 prefix-strip-retry resolution order
    exists in ONE spot (previously duplicated across the two functions, a
    silent-drift risk if the order ever changed in only one place).
    ``fallback_reason`` is ``None`` when a real value was resolved (config
    override or catalog hit) — truthy iff the 128K fallback fired, same
    role the old ``is_fallback`` bool played, now carrying WHICH of
    #4680②'s two states produced it.

    #4689: an operator-declared config override (registered via
    ``register_max_input_overrides``) wins UNCONDITIONALLY — checked
    before the catalog lookup, not as a fallback for when the catalog
    lookup fails (owner instruction: "catalog が外れた時だけ、ではない")."""
    config_override = _config_max_input_overrides.get(model)
    if config_override is not None:
        return config_override, f"config: llm.models.<tier>.max_input_tokens ({model})", None

    max_input, reason = _lookup_max_input(model)
    if max_input is not None:
        return max_input, f"litellm catalog: {model}", None

    # #1162: provider-prefixed proxy models miss the catalog under the prefix
    # but resolve under the bare model name. e.g. ``openai/gemini-2.5-flash-lite``
    # (routing Gemini through an openai-compat proxy) is not in litellm's openai
    # catalog → exception → would fall to the 128K default, over-compacting a
    # real 1M window by ~87%. Strip the FIRST provider segment and retry before
    # falling back. This only IMPROVES resolution: a still-unknown bare name
    # falls through to the same conservative fallback as before (safe even if a
    # proxy aliases the prefixed name to something else — the bare lookup just
    # misses → 128K).
    if "/" in model:
        bare = model.split("/", 1)[1]
        max_input, reason = _lookup_max_input(bare)
        if max_input is not None:
            return max_input, f"litellm catalog: {bare}", None

    reason_text = (
        "litellm not yet loaded in this process"
        if reason is MaxInputTokensFallbackReason.NOT_READY
        else "model not cataloged"
    )
    return (
        _FALLBACK_MAX_INPUT_TOKENS,
        f"reyn fallback default: {_FALLBACK_MAX_INPUT_TOKENS:,} tokens ({reason_text})",
        reason,
    )


def get_max_input_tokens(
    model: str,
    *,
    events: "EventLog | None" = None,
    phase: str | None = None,
    run_id: str | None = None,
) -> int:
    """Return the maximum input token budget for *model*.

    Queries LiteLLM's model catalog (`litellm.get_model_info`). If the model
    is not recognized, returns the conservative default (_FALLBACK_MAX_INPUT_TOKENS)
    and emits a ``model_budget_fallback`` observability event.

    Parameters
    ----------
    model:
        The LiteLLM model string (e.g. ``"gemini/gemini-2.5-flash-lite"``).
    events:
        Optional EventLog for emitting observability events. When None, the
        fallback warning is logged via the standard logger instead.
    phase:
        Phase name for the observability event payload.
    run_id:
        Run ID for the observability event payload.

    Returns
    -------
    int
        Positive integer token count. Always > 0.
    """
    value, _source, reason = _resolve_max_input(model)
    prior_reason = _warned_models.get(model)

    if reason is not None and prior_reason != reason:
        # #4680②: warn on a FRESH reason for this model — first-ever
        # fallback (prior_reason is None), OR a NOT_READY -> UNCATALOGED
        # transition (litellm finished loading and this model turned out
        # genuinely uncataloged; see the module docstring for why the
        # reverse transition never happens once litellm is ready).
        _warned_models[model] = reason
        if reason is MaxInputTokensFallbackReason.NOT_READY:
            msg = (
                f"model_budget: litellm has not finished loading in this "
                f"process yet, so model={model!r}'s real context window is "
                f"not known — using conservative fallback of "
                f"{_FALLBACK_MAX_INPUT_TOKENS:,} tokens (temporary; will "
                f"self-correct once litellm is ready)"
            )
        else:
            msg = (
                f"model_budget: max_input_tokens unknown for model={model!r} "
                f"— litellm has no catalog entry for it (checked after "
                f"litellm finished loading); using conservative fallback of "
                f"{_FALLBACK_MAX_INPUT_TOKENS:,} tokens (permanent for this "
                f"process unless llm.models.<tier>.max_input_tokens is "
                f"configured, or litellm's own catalog is updated)"
            )
        logger.warning(msg)
        if events is not None:
            events.emit(
                "model_budget_fallback",
                model=model,
                fallback_tokens=_FALLBACK_MAX_INPUT_TOKENS,
                reason=reason.value,
                phase=phase,
                run_id=run_id,
            )
    elif reason is None and prior_reason is not None:
        # #4680②: THE correction — a model previously warned as NOT_READY
        # (the only reason that can precede a real value; see the module
        # docstring) has now resolved to a real value once litellm became
        # ready. "Warned once, never corrected" (#4680's own reported
        # symptom) is wrong specifically for this temporary case — the
        # permanent UNCATALOGED case never reaches here (reason stays
        # UNCATALOGED forever, so the `reason is not None` branch above,
        # not this one, handles every subsequent call for it). Log only
        # (info, not warning — this is good news), not a NEW audit event:
        # `model_budget_fallback`'s own name means "a fallback occurred",
        # and none is occurring at this call.
        del _warned_models[model]
        logger.info(
            f"model_budget: max_input_tokens for model={model!r} is now "
            f"resolved via litellm catalog to {value:,} tokens (previously "
            f"used the temporary NOT_READY fallback of "
            f"{_FALLBACK_MAX_INPUT_TOKENS:,})"
        )
    return value


def get_max_input_tokens_source(model: str) -> str:
    """Human-readable description of where ``get_max_input_tokens(model)``'s
    value came from — litellm's catalog, or this module's conservative
    fallback (there is no user-configurable override of a model's context
    window anywhere in reyn today). Display-only (status bar / debug)."""
    _value, source, _reason = _resolve_max_input(model)
    return source
