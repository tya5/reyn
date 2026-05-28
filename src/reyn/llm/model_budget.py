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
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import litellm

if TYPE_CHECKING:
    from reyn.events.events import EventLog

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
    try:
        info = litellm.get_model_info(model)
        max_input = info.get("max_input_tokens")
        if max_input and int(max_input) > 0:
            return int(max_input)
        # Model is in catalog but max_input_tokens is None/0 — fall through.
    except Exception:
        pass  # Not in catalog — fall through to default.

    # Fallback path: emit warning once per model.
    if model not in _warned_models:
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

    return _FALLBACK_MAX_INPUT_TOKENS
