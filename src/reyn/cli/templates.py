"""Static templates and metadata used by `reyn init` and `reyn config`."""
from __future__ import annotations

REYN_YAML_TEMPLATE = """\
# Reyn project configuration — commit this file.
# Local overrides belong in reyn.local.yaml (gitignored) — never commit secrets here.

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

# ───────────────────────────────────────────────────────────────────────────
# Pre-approved permissions for chat / skill ops.
#
# `file.read` recursive under the project root is on by default so that
# `reyn chat` can answer questions about the codebase without prompting on
# every file. Same convention as Claude Code / aider / Cursor: read freely
# in cwd, ask before writing.
#
# ⚠ SECURITY NOTE: file.read recursive means the LLM can request any file
# under the project root — including `.env`, secret keys, draft notes, etc.
# Their contents will be sent to your model provider. Mitigate by either:
#   - Keeping secrets out of the project root (recommended).
#   - Tightening this list to specific paths (e.g. `src/`, `docs/`).
#   - Removing this block entirely; chat will then prompt for each file.
#
# `file.write` and `shell` stay opt-in — they prompt interactively, or the
# operator can pre-approve specific paths here.
# ───────────────────────────────────────────────────────────────────────────
permissions:
  python.safe: allow
  file.read:
    - path: "."
      scope: recursive
  # ── file.write — opt-in, by-path ────────────────────────────────────────
  # Reads are recursive by default (above); writes are not. Without an
  # entry here the chat agent triggers an interactive permission prompt
  # for each write (and fails on headless / non-TTY runs). Uncomment one
  # or more entries to silently allow writes to specific subtrees you've
  # decided are safe to overwrite. The prompt itself also offers a
  # "remember this path" button that persists into `.reyn/approvals.yaml`
  # (gitignored) — so the allow-list can be built up interactively rather
  # than pre-declared.
  #
  # file.write:
  #   - path: "scratch/"
  #     scope: recursive
  #   - path: "drafts/"
  #     scope: recursive
  #
  # ── web.fetch — operator opt-in ─────────────────────────────────────────
  # `web_search` (DuckDuckGo public query) is always on. `web_fetch`
  # (arbitrary URL) is off by default: the LLM could bake secrets into
  # a URL and have an attacker-controlled server log them, so we don't
  # want this one on without a deliberate decision. Uncomment if you
  # want the agent to be able to read specific pages after searching:
  # web.fetch: allow
  #
  # ── shell — keep off unless you're writing meta-skills ──────────────────
  # `shell: allow` lets skills run arbitrary subprocesses. Required for
  # specific meta-skills (skill_builder, skill_improver) but unsafe for
  # a chat agent — keep commented out unless you understand the
  # implications.
  # shell: allow

# ───────────────────────────────────────────────────────────────────────────
# MCP servers (optional). Stdlib skills that depend on MCP need a
# `filesystem` server. Uncomment below to enable, or see the full example at
# cookbook/configs/with-mcp.yaml.  Run the server manually first to verify:
#   npx -y @modelcontextprotocol/server-filesystem .
# Full setup guide: docs/guide/for-skill-authors/use-an-mcp-server.md
# ───────────────────────────────────────────────────────────────────────────
# mcp:
#   servers:
#     filesystem:
#       type: stdio
#       command: npx
#       args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
#     # git:
#     #   type: stdio
#     #   command: npx
#     #   args: ["-y", "@modelcontextprotocol/server-git", "--repository", "."]
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
        "scope":   "reyn.yaml / reyn.local.yaml",
        "desc":    "Default model class used when a phase has no model_class.",
        "values":  "light | standard | strong  (resolved via models map)",
        "example": "model: standard",
    },
    {
        "key":     "models",
        "default": "{}",
        "scope":   "reyn.yaml / reyn.local.yaml",
        "desc":    "Map of model class names to LiteLLM model strings.",
        "values":  "dict: class_name → litellm_model_string",
        "example": "models:\n  light:    openai/gpt-4o-mini\n  standard: openai/gpt-4o\n  strong:   openai/o3",
    },
    {
        "key":     "api_base",
        "default": "(none)",
        "scope":   "reyn.local.yaml  (keep out of git)",
        "desc":    "LiteLLM proxy base URL. Set this if you route requests through a local proxy.",
        "values":  "URL string",
        "example": "api_base: http://localhost:4000",
    },
    {
        "key":     "output_language",
        "default": "ja",
        "scope":   "reyn.yaml / reyn.local.yaml",
        "desc":    "Language code injected into the context frame for all LLM outputs.",
        "values":  "BCP-47 language tag (e.g. en, ja, zh)",
        "example": "output_language: en",
    },
    {
        "key":     "shell_allowed",
        "default": "false",
        "scope":   "reyn.yaml / reyn.local.yaml",
        "desc":    "Allow the shell Control IR op globally. Equivalent to --allow-shell on every run.",
        "values":  "true | false",
        "example": "shell_allowed: false",
    },
    {
        "key":     "permissions",
        "default": "{}",
        "scope":   "reyn.yaml / reyn.local.yaml",
        "desc":    "Pre-approve specific Control IR ops without interactive prompts.",
        "values":  "dict: op_kind → 'allow'",
        "example": "permissions:\n  shell: allow",
    },
]
