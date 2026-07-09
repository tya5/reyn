---
type: landing
audience: [human, agent]
---

# reyn

**An operating system for LLM agents — they decide, organize, and orchestrate; the OS makes every action typed, permissioned, audited, and recoverable by construction.**

reyn lets an LLM agent decide, organize, and orchestrate — picking tools, spawning sub-agents, composing pipelines — while the OS keeps every action typed (Control IR), permission-gated, audit-logged, and recoverable from a crash. The agent's freedom is real; the OS's guarantees are structural, not a matter of the agent's discipline.

## Where to start

| If you want to... | Go to |
|---|---|
| Install and start the agent | [Getting started](guide/getting-started/01-installation.md) |
| Solve a specific problem (chat user) | [Guide / for users](guide/for-users/manage-permissions.md) |
| Understand the design | Concepts |
| Read reyn through agent-engineering lenses | [Eight lenses](concepts/agent-engineering/index.md) |
| Contribute | See `docs/deep-dives/contributing/` in the repository |

## The four reading modes (Diátaxis)

- **Guide** — task-oriented. *Getting started* for onboarding, *for users* for chat-mode usage.
- **Reference** — information-oriented. Look up and leave.
- **Concepts** — understanding-oriented. The "why" of reyn.

## Project status

reyn is in alpha (0.1.x). The DSL, CLI, and event log are stable enough to build workflows against, but APIs may still shift. Changelog and roadmap pages are coming in a later docs phase (`changelog.md`, `architecture/roadmap.md`).

## Powered by AI

Reyn is powered by AI in two senses:

- **At runtime.** Every agent run delegates decisions to an LLM provider via LiteLLM. Reyn is an agent OS where agency is bounded by construction — decide, spawn, orchestrate, but only through typed, permissioned, auditable, rewindable ops.
- **In its development.** Substantial portions of the codebase, the stdlib components, this documentation, and the landing page were drafted with AI tooling — primarily Claude Code (Anthropic) for implementation and Claude Design for the website. Human review, integration, and final architectural calls live with the maintainer; AI contributions are recorded as `Co-Authored-By: Claude ...` trailers in the git history.

This is a transparency note, not a marketing line. For provenance auditing, see `git log --grep="Co-Authored-By: Claude"` and the design prompts under `website/_design/`.
