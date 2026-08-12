"""Frozen snapshot of the built-in model catalog #4349 removes (`reyn config
migrate` only — never imported by the runtime resolution path).

#4349 deletes ``reyn.llm.builtin_models.BUILTIN_MODELS`` and the "resolve a
bare class name against a built-in catalog" behavior it powered — an
operator's ``reyn.yaml`` referencing a catalog shorthand by name (e.g.
``models: {standard: claude-sonnet-thinking}``, or ``extends:
claude-sonnet``) resolves successfully today and would silently fail (or
resolve to nothing) once the catalog is gone.

Per architect ruling on #4349 (relayed via lead-coder): the migration path is
a hard requirement of the removal, not an optional follow-up, and the
correspondence table it depends on may ONLY live here — ``reyn config
migrate`` is the one caller allowed to know what these old shorthand names
used to mean. It is a DEAD, ONE-TIME snapshot of ``BUILTIN_MODELS`` as it
stood immediately before #4349 deleted it — not re-exported, not read by any
resolution code path, and not kept in sync with anything going forward (the
whole point of the removal is that reyn stops tracking a third-party
catalog). A future model naming change on any provider's side does NOT
update this file; it exists only so `reyn config migrate` can tell an
operator what their OLD shorthand used to mean.

Tier aliases (``light``/``standard``/``strong``) are deliberately EXCLUDED —
those were never something an operator wrote as a `models:` VALUE (writing
``standard: standard`` would be circular); they were the zero-config
fallback `models:` block's own KEYS, which `reyn.yaml`'s project template
already declares literally. Only the named catalog entries an operator could
plausibly have referenced by shorthand — as a `models:` tier's value or an
`extends:` target — are captured here.
"""
from __future__ import annotations

#: old catalog key -> the structured value an operator's `models:` tier (or
#: `extends:` target) should become. Frozen at #4349; see module docstring.
LEGACY_MODEL_CATALOG: "dict[str, dict]" = {
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
    "gpt-4o-mini": {"model": "openai/gpt-4o-mini"},
    "gpt-4o": {"model": "openai/gpt-4o"},
    "gemini-flash-lite": {
        "model": "gemini/gemini-2.5-flash-lite",
        "reasoning_effort": "low",
    },
    "gemini-pro": {
        "model": "gemini/gemini-2.5-pro",
        "reasoning_effort": "medium",
    },
    "gemini-3.1-flash-preview": {
        "model": "gemini/gemini-3.1-flash-preview",
        "reasoning_effort": "low",
    },
    "gemini-2.0-flash": {
        "model": "gemini/gemini-2.0-flash",
        "extra_body": {
            "thinking_config": {"thinking_budget": 0},
        },
    },
}
