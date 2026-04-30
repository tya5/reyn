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
