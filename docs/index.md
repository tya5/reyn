---
type: landing
audience: [human, agent]
---

# reyn

**LLM workflow OS — phase transitions as a constrained decision graph.**

reyn runs LLM workflows as a state machine. Phases are stateless and reusable; a Skill defines the graph and the final output schema; the OS controls execution. The LLM's role is reduced to choosing among OS-provided transitions and producing structured artifacts — never to inventing control flow.

## Where to start

| If you want to... | Go to |
|---|---|
| Install and run your first skill | [Getting started](guide/getting-started/01-installation.md) |
| Solve a specific problem (chat user) | [Guide / for users](guide/for-users/manage-permissions.md) |
| Solve a specific problem (skill author) | [Guide / for skill authors](guide/for-skill-authors/validate-artifacts.md) |
| Look up exact behavior | [Reference](reference/cli/run.md) |
| Understand the design | [Concepts](concepts/principles.md) |
| Read reyn through agent-engineering lenses | [Seven lenses](guide/for-skill-authors/agent-engineering/index.md) |
| Read LLM-targeted rubrics | [Skill-builder checklist](guide/for-skill-authors/skill-builder-checklist.md) |
| Contribute | See `docs/deep-dives/contributing/` in the repository |

## The four reading modes (Diátaxis)

- **Guide** — task-oriented. *Getting started* for onboarding, *for users* for chat-mode usage, *for skill authors* for building skills.
- **Reference** — information-oriented. Look up and leave.
- **Concepts** — understanding-oriented. The "why" of reyn.

LLM-targeted rubrics (skill-builder checklist, eval-builder rubric, etc.) live under `guide/for-skill-authors/` — their primary reader is a reyn skill (e.g. `skill_builder`), but humans read them too.

## Project status

reyn is in alpha (0.1.x). The DSL, CLI, and event log are stable enough to build skills against, but APIs may still shift. Changelog and roadmap pages are coming in a later docs phase (`changelog.md`, `architecture/roadmap.md`).

## Powered by AI

Reyn is powered by AI in two senses:

- **At runtime.** Every Skill execution delegates decisions to an LLM provider via LiteLLM. Reyn is an LLM workflow OS by design.
- **In its development.** Substantial portions of the codebase, the stdlib skills, this documentation, and the landing page were drafted with AI tooling — primarily Claude Code (Anthropic) for implementation and Claude Design for the website. Human review, integration, and final architectural calls live with the maintainer; AI contributions are recorded as `Co-Authored-By: Claude ...` trailers in the git history.

This is a transparency note, not a marketing line. For provenance auditing, see `git log --grep="Co-Authored-By: Claude"` and the design prompts under `website/_design/`.
