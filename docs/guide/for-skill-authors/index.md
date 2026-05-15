---
type: landing
topic: skill-authoring
audience: [human]
---

# For skill authors

Task-oriented how-tos for building skills, agents, and the runtime around them. The pages are grouped by what you're trying to do — pick the cluster that matches your task.

If you haven't already, work through [Getting started](../getting-started/01-installation.md) first — these how-tos assume you have Reyn installed and have at least skimmed [Concepts: phase vs skill vs OS](../../concepts/phase-vs-skill-vs-os.md).

## Foundation

Start here if you've finished the tutorials and want to author a skill yourself.

- **[Write your first custom skill](write-your-first-custom-skill.md)** — build a skill from scratch by hand. Walk through `skill.md`, `phases/<name>.md`, and `artifacts/<name>.yaml` with a complete worked example.
- **[Import an existing skill](import-an-existing-skill.md)** — bring a prompt or another framework's spec into Reyn's DSL using `skill_importer`.

## Composition & multi-agent

How skills compose with each other and with peer agents.

- **[Compose skills with `run_skill`](compose-skills-with-run-skill.md)** — call one skill from inside another.
- **[Iterate with fan-out](iterate-with-fan-out.md)** — apply a sub-step over a list and collect results.
- **[Build an agent team](build-an-agent-team.md)** — set up multiple agents with role-specific skill allowlists.
- **[Multi-hop delegation](multi-hop-delegation.md)** — chain delegations through more than one agent.
- **[Use plan mode for multi-step tasks](use-plan-mode.md)** — decompose a complex chat request into async steps with crash recovery and operator escape hatches.
- **[Restrict agent skills](restrict-agent-skills.md)** — scope what each agent in a team can run.

## Phase mechanics

Deterministic controls inside a phase — pre-LLM steps, schema validation, state persistence.

- **[Add a Python preprocessor](add-a-python-preprocessor.md)** — `safe` vs `unsafe` mode, signatures, sandbox boundaries.
- **[Validate artifacts](validate-artifacts.md)** — strict-mode checks and schema patterns.
- **[Persist state](persist-state.md)** — what survives across runs and how the workspace stores it.

## Reliability

Running skills in production — crash recovery, auditing, cost control.

- **[Crash recovery and resume](crash-recovery-and-resume.md)** — how the WAL + forward-replay works, how to control resume behaviour, and when to reset. **Start here before deploying to production.**
- **[Audit and explainability](audit-and-explainability.md)** — reading the events log, what it proves, and how to use it for compliance or internal review.

## Operations

Debugging, integration with external services.

- **[Debug with events](debug-with-events.md)** — read the JSONL log to understand what happened.
- **[Use an MCP server](use-an-mcp-server.md)** — wire an external tool into a phase via the `mcp` Control IR op.

## UX & polish

User-facing surface area for finished skills.

- **[Author a design](author-a-design.md)** — Claude Design integration for visual artifacts.
- **[Localize output](localize-output.md)** — `output_language` and per-phase locale handling.
- **[Enable voice input](enable-voice-input.md)** — voice-driven chat mode.

## Working with stdlib authoring tools

Rubrics and reference material for the LLM-driven authoring stdlib (`skill_builder`, `skill_improver`, `skill_importer`, `eval_builder`). Their primary reader is a Reyn skill, but humans read them too when something goes wrong.

- **[skill-builder checklist](skill-builder-checklist.md)**
- **[eval-builder rubric](eval-builder-rubric.md)**
- **[skill-importer mapping](skill-importer-mapping.md)**
- **[skill-improver criteria](skill-improver-criteria.md)**
- **[Glossary](glossary.md)** — vocabulary used across the rubrics.

## See also

- [Concepts](../../concepts/principles.md) — the "why" behind the patterns these how-tos use.
- [Reference / DSL](../../reference/dsl/skill-md.md) — exact frontmatter and YAML schemas.
- [Reference / CLI](../../reference/cli/run.md) — `reyn run`, `reyn lint`, `reyn eval`, `reyn chat`.
