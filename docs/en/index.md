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
| Install and run your first skill | [Tutorials](tutorials/01-installation.md) |
| Solve a specific problem | [How-to](how-to/validate-artifacts.md) |
| Look up exact behavior | [Reference](reference/cli/run.md) |
| Understand the design | [Concepts](concepts/principles.md) |
| Read reyn through agent-engineering lenses | [Seven lenses](concepts/agent-engineering/index.md) |
| Read agent-only docs | [/agent/](../agent/README.md) |
| Contribute | [Contributing](contributing/style-guide.md) |

## The four reading modes (Diátaxis)

- **Tutorials** — learning-oriented, ordered. Read these first.
- **How-to** — task-oriented recipes. Skim by problem.
- **Reference** — information-oriented. Look up and leave.
- **Concepts** — understanding-oriented. The "why" of reyn.

`agent/` is a fifth mode reserved for documents whose primary reader is a reyn skill (e.g. `skill_builder`).

## Project status

reyn is in alpha (0.1.x). The DSL, CLI, and event log are stable enough to build skills against, but APIs may still shift. Changelog and roadmap pages are coming in a later docs phase (`changelog.md`, `architecture/roadmap.md`).

## Powered by AI

Reyn is powered by AI in two senses:

- **At runtime.** Every Skill execution delegates decisions to an LLM provider via LiteLLM. Reyn is an LLM workflow OS by design.
- **In its development.** Substantial portions of the codebase, the stdlib skills, this documentation, and the landing page were drafted with AI tooling — primarily Claude Code (Anthropic) for implementation and Claude Design for the website. Human review, integration, and final architectural calls live with the maintainer; AI contributions are recorded as `Co-Authored-By: Claude ...` trailers in the git history.

This is a transparency note, not a marketing line. For provenance auditing, see `git log --grep="Co-Authored-By: Claude"` and the design prompts under `website/_design/`.
