# Reyn Project Context

This file is auto-injected into the system prompt for every phase via
`project_context_path` in `reyn.yaml`. Put project-wide background here that
all skills should implicitly know — domain glossary, conventions, references.

## About this project

Reyn is an LLM-driven phase execution engine with a Markdown DSL.
The runtime constrains LLM autonomy with closed candidate transitions,
JSON-schema validated outputs, and per-phase permission scopes — see
`CLAUDE.md` for the full architectural contract.

## Conventions

- Replies in chat default to Japanese (`output_language: ja`) unless the
  user writes in another language; mirror their language and register.
- Treat user-provided file paths as absolute unless explicitly relative.
- Costs and token usage are tracked per-run; prefer terse phrasing in
  responses unless detail is requested.
