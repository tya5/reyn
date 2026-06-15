"""Built-in model catalog for Reyn.

Provides BUILTIN_MODELS: a flat namespace of pre-defined model configurations
that operators can reference via ``extends`` or as shorthand class references
(str form without ``/``) in ``reyn.yaml``.

Operators may override any built-in entry by declaring the same name under
``models:`` in their ``reyn.yaml``.  User-declared entries always take
precedence over built-ins.

Note on ``max_completion_tokens``:
    Built-in entries use ``max_completion_tokens`` (the OpenAI o1+ enforced
    parameter) rather than the legacy ``max_tokens`` (a soft hint that many
    providers ignore).  Operators should prefer ``max_completion_tokens`` for
    hard cost control.  See the documentation for details.

Note on Gemini thinking syntax:
    ``gemini-2.0-flash`` passes ``extra_body.thinking_config.thinking_budget=0``
    to disable thinking mode and reduce cost.  This syntax is routed via
    LiteLLM's OpenAI-compatible shim; if Gemini / LiteLLM changes the
    parameter name in a future release, update this entry and the docs.
"""
from __future__ import annotations

BUILTIN_MODELS: dict[str, dict] = {
    # -------------------------------------------------------------------------
    # Anthropic
    # -------------------------------------------------------------------------
    "claude-sonnet": {
        "model": "anthropic/claude-3-7-sonnet",
        "max_completion_tokens": 8192,
    },
    "claude-sonnet-thinking": {
        "model": "anthropic/claude-3-7-sonnet",
        "max_completion_tokens": 16000,
        "extra_body": {
            "thinking": {"type": "enabled", "budget_tokens": 8000},
        },
    },
    "claude-haiku": {
        "model": "anthropic/claude-3-5-haiku",
        "max_completion_tokens": 4096,
    },
    # -------------------------------------------------------------------------
    # OpenAI
    # -------------------------------------------------------------------------
    "gpt-4o-mini": {"model": "openai/gpt-4o-mini"},
    "gpt-4o": {"model": "openai/gpt-4o"},
    # -------------------------------------------------------------------------
    # Gemini  (gemini/ prefix = correct litellm catalog lookup → 1M context)
    # -------------------------------------------------------------------------
    # #1654: reasoning ON by default (out-of-box) — reasoning_effort maps to the
    # provider's native thinking budget (low→1024, medium→2048; verified live via
    # the litellm proxy: reasoning_content text exposed). Capture + display +
    # cross-turn continuity are config-default-ON (chat.reasoning); this default
    # makes the model actually produce reasoning so the feature works without
    # operator config. Cost note: thinking tokens add to spend — operators who
    # want it off set `reasoning_effort: none` (or `disable`) on the model, or
    # `chat.reasoning.display: false` to keep the budget but hide the text.
    # Only the gemini reasoning models get a default; gpt-4o* (non-reasoning) +
    # gemini-2.0-flash (thinking_budget=0, mutually exclusive) are left alone.
    "gemini-flash-lite": {"model": "gemini/gemini-2.5-flash-lite", "reasoning_effort": "low"},
    "gemini-pro": {"model": "gemini/gemini-2.5-pro", "reasoning_effort": "medium"},
    "gemini-3.1-flash-preview": {"model": "gemini/gemini-3.1-flash-preview", "reasoning_effort": "low"},
    "gemini-2.0-flash": {
        "model": "gemini/gemini-2.0-flash",
        "extra_body": {
            # Disable thinking to reduce cost.  Syntax is LiteLLM/Gemini API
            # specific — verify against LiteLLM release notes if behavior
            # changes after a provider API update.
            "thinking_config": {"thinking_budget": 0},
        },
    },
}
