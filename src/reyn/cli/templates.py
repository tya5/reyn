"""Static templates and metadata used by `reyn init` and `reyn config`."""
from __future__ import annotations


REYN_YAML_TEMPLATE = """\
# Reyn project configuration — commit this file.
# Local overrides belong in .reyn/config.yaml (gitignored) — never commit secrets here.

# Default model class when --model is not specified.
model: standard

# Model class → LiteLLM model string.
# Three standard tiers. Edit to match your provider.
models:
  light:    openai/gpt-4o-mini
  standard: openai/gpt-4o
  strong:   openai/gpt-4o

# output_language: en          # en | ja | zh | ...
# shell_allowed: false         # allow 'shell' Control IR op (meta-apps only)
"""


REYN_LOCAL_CONFIG_TEMPLATE = """\
# Local environment overrides — gitignored, never commit.

# LiteLLM proxy base URL (omit if calling providers directly)
# api_base: http://localhost:4000

# API keys must be set as environment variables, not here:
#   export OPENAI_API_KEY=sk-...
#   export ANTHROPIC_API_KEY=sk-ant-...
#   export GEMINI_API_KEY=...

# Override model mappings for your local setup (optional)
# models:
#   light:    openai/gemini-2.5-flash-lite
#   standard: openai/gemini-2.5-flash-lite
#   strong:   openai/gemini-2.5-flash-lite
"""


CONFIG_FIELDS: list[dict] = [
    {
        "key":     "model",
        "default": "standard",
        "scope":   "reyn.yaml / .reyn/config.yaml",
        "desc":    "Default model class used when a phase has no model_class.",
        "values":  "light | standard | strong  (resolved via models map)",
        "example": "model: standard",
    },
    {
        "key":     "models",
        "default": "{}",
        "scope":   "reyn.yaml / .reyn/config.yaml",
        "desc":    "Map of model class names to LiteLLM model strings.",
        "values":  "dict: class_name → litellm_model_string",
        "example": "models:\n  light:    openai/gpt-4o-mini\n  standard: openai/gpt-4o\n  strong:   openai/o3",
    },
    {
        "key":     "api_base",
        "default": "(none)",
        "scope":   ".reyn/config.yaml  (keep out of git)",
        "desc":    "LiteLLM proxy base URL. Set this if you route requests through a local proxy.",
        "values":  "URL string",
        "example": "api_base: http://localhost:4000",
    },
    {
        "key":     "output_language",
        "default": "ja",
        "scope":   "reyn.yaml / .reyn/config.yaml",
        "desc":    "Language code injected into the context frame for all LLM outputs.",
        "values":  "BCP-47 language tag (e.g. en, ja, zh)",
        "example": "output_language: en",
    },
    {
        "key":     "shell_allowed",
        "default": "false",
        "scope":   "reyn.yaml / .reyn/config.yaml",
        "desc":    "Allow the shell Control IR op globally. Equivalent to --allow-shell on every run.",
        "values":  "true | false",
        "example": "shell_allowed: false",
    },
    {
        "key":     "permissions",
        "default": "{}",
        "scope":   "reyn.yaml / .reyn/config.yaml",
        "desc":    "Pre-approve specific Control IR ops without interactive prompts.",
        "values":  "dict: op_kind → 'allow'",
        "example": "permissions:\n  shell: allow",
    },
]
