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
# MCP servers (= op-managed, not edited here).
#
# Issue #470 (2026-05-22): MCP server registry lives in `.reyn/mcp.yaml`, not
# in this file. The split matches the principle that `reyn.yaml` should carry
# only **static deployment config** (= edit + restart to apply), while
# runtime-mutable state lives under `.reyn/`. Sister convention to
# `.reyn/approvals.yaml` (= dynamic permission state, ops-managed).
#
# Install / remove MCP servers via the CLI; the ops write to `.reyn/mcp.yaml`:
#   reyn mcp install io.github.modelcontextprotocol/server-filesystem
#   reyn mcp drop filesystem
#
# Migration: if you have legacy `mcp.servers` entries here from before #470,
# they continue to load. Run `reyn config migrate-mcp` (or `--dry-run` first)
# to move them to `.reyn/mcp.yaml` and remove from this file.
#
# Full setup guide: docs/guide/for-skill-authors/use-an-mcp-server.md
# ───────────────────────────────────────────────────────────────────────────
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
