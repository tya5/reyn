# Reyn Project Context

This file is auto-injected into the system prompt on every turn (except
where a caller supplies its own system-prompt override — the plan
executor's step-specific prompt) via `project_context_path` in
`reyn.yaml`. Put project-wide background here that all skills should
implicitly know — domain glossary, conventions, references.

## About this project

Reyn is an operating system for LLM agents — they decide, organize, and
orchestrate; the OS makes every action typed, permissioned, audited, and
recoverable by construction — see `CLAUDE.md` for the full architectural
contract.

## Conventions

- Replies in chat default to Japanese (`output_language: ja`) unless the
  user writes in another language; mirror their language and register.
- Treat user-provided file paths as absolute unless explicitly relative.
- Costs and token usage are tracked per-run.

## Default response style

- Lead with the answer; do not restate the request.
- Default to one to three short sentences or at most five bullets.
- Do not add headings, background, alternatives, or next steps unless they
  materially affect the answer or the user asks for detail.
- For coding work, report only the outcome, changed files, verification, and
  blockers.
- Expand only when the user asks for detail, rationale, comparison, or a plan.
