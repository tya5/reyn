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

# Emit the fallback warning at most once per process per model string so noisy
# repeated calls don't flood logs. Keyed by model string.
_warned_models: set[str] = set()

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


def _lookup_max_input(model: str) -> "int | None":
    """Return ``max_input_tokens`` from LiteLLM's catalog for *model*, or None
    when the model is unrecognized or has no positive window.

    No fallback, no events — pure catalog lookup so callers can compose retries
    (e.g. provider-prefix-strip, #1162).
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
        from reyn.llm.litellm_bootstrap import ensure_litellm_ready_or_defer
        litellm = ensure_litellm_ready_or_defer()
        info = litellm.get_model_info(model)
        max_input = info.get("max_input_tokens")
        if max_input and int(max_input) > 0:
            return int(max_input)
    except Exception:
        pass  # Not in catalog / no positive window.
    return None


def _resolve_max_input(model: str) -> "tuple[int, str, bool]":
    """Resolve (value, source, is_fallback) exactly once — the single place
    ``get_max_input_tokens`` and ``get_max_input_tokens_source`` both delegate
    to, so the #1162 prefix-strip-retry resolution order exists in ONE spot
    (previously duplicated across the two functions, a silent-drift risk if
    the order ever changed in only one place).

    #4689: an operator-declared config override (registered via
    ``register_max_input_overrides``) wins UNCONDITIONALLY — checked
    before the catalog lookup, not as a fallback for when the catalog
    lookup fails (owner instruction: "catalog が外れた時だけ、ではない")."""
    config_override = _config_max_input_overrides.get(model)
    if config_override is not None:
        return config_override, f"config: llm.models.<tier>.max_input_tokens ({model})", False

    max_input = _lookup_max_input(model)
    if max_input is not None:
        return max_input, f"litellm catalog: {model}", False

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
        max_input = _lookup_max_input(bare)
        if max_input is not None:
            return max_input, f"litellm catalog: {bare}", False

    return (
        _FALLBACK_MAX_INPUT_TOKENS,
        f"reyn fallback default: {_FALLBACK_MAX_INPUT_TOKENS:,} tokens (model not cataloged)",
        True,
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
    value, _source, is_fallback = _resolve_max_input(model)
    if is_fallback and model not in _warned_models:
        _warned_models.add(model)
        msg = (
            f"model_budget: max_input_tokens unknown for model={model!r}; "
            f"using conservative fallback of {_FALLBACK_MAX_INPUT_TOKENS:,} tokens"
        )
        logger.warning(msg)
        if events is not None:
            events.emit(
                "model_budget_fallback",
                model=model,
                fallback_tokens=_FALLBACK_MAX_INPUT_TOKENS,
                phase=phase,
                run_id=run_id,
            )
    return value


def get_max_input_tokens_source(model: str) -> str:
    """Human-readable description of where ``get_max_input_tokens(model)``'s
    value came from — litellm's catalog, or this module's conservative
    fallback (there is no user-configurable override of a model's context
    window anywhere in reyn today). Display-only (status bar / debug)."""
    _value, source, _is_fallback = _resolve_max_input(model)
    return source
