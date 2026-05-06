# Reyn

**LLM workflow OS — predictable, auditable, constrained.**

[![CI](https://github.com/tya5/reyn/actions/workflows/test.yml/badge.svg)](https://github.com/tya5/reyn/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

```bash
pip install reyn          # not yet on PyPI — see Quick Start below for local install
reyn init
reyn run my_skill "Write a report on AI in education."
```

---

## Why Reyn

- **Predictability over autonomy.** The LLM picks only from OS-provided transitions — it cannot invent control flow. Every run is auditable and replayable from an append-only event log.
- **Constrained reasoning, not magic.** Skills are plain Markdown + YAML. The OS handles context build, validation, routing, and retries. You write domain logic; the engine enforces the contract.
- **Multi-agent without chaos.** Sub-skills compose into larger pipelines through a typed artifact channel. Each hop emits events with the same chain ID — a distributed trace across agents is a single `grep`.

---

## Quick Start

**Requirements:** Python 3.11+, a [LiteLLM](https://github.com/BerriAI/litellm)-compatible model endpoint.

```bash
pip install -e ".[rich]"       # local install; web UI: pip install -e ".[rich,web]"
export OPENAI_API_KEY=sk-...   # or set the key for your LiteLLM proxy
reyn init                      # creates reyn.yaml + .reyn/config.yaml
```

Create a minimal skill under `reyn/local/my_skill/`:

```yaml
# artifacts/request.yaml
name: request
wrapped: false
schema:
  type: object
  properties:
    topic: {type: string}
  required: [topic]
```

```markdown
<!-- phases/draft.md -->
---
type: phase
name: draft
input: request
---

Write a concise summary on the topic.
```

```markdown
<!-- skill.md -->
---
type: skill
name: my_skill
entry: draft
final_output: draft_result
graph:
  draft: []
---
```

Run it:

```bash
reyn run my_skill '{"type":"request","data":{"topic":"AI in education"}}'
# or pass natural language directly:
reyn run my_skill "Summarize AI trends in education."
```

Full tutorial: [docs/en/tutorials/02-your-first-skill.md](docs/en/tutorials/02-your-first-skill.md)

---

## Architecture

The OS is the constant. Skills come and go; OS code never changes when a new skill is added.

```
User → Agent → Skill → OS → Phase → Workspace
                         \-> Event  (append-only, replayable)
```

| Layer | Role |
|---|---|
| **Skill** | Phase graph + final output schema, defined in Markdown/YAML |
| **Phase** | Stateless unit — input schema + instructions only |
| **OS** | Runtime: context build, LLM call, validation, Control IR, transitions |
| **Workspace** | Single source of truth for all artifacts and files |
| **Event** | Structured record of every state change; drives audit and replay |

The LLM is a constrained decision engine: it picks a next phase from the OS-provided candidate list and produces a typed artifact. It cannot hallucinate a transition or bypass validation.

Details: [docs/en/concepts/architecture.md](docs/en/concepts/architecture.md) | [docs/en/concepts/principles.md](docs/en/concepts/principles.md)

---

## Documentation

Full docs live under `docs/` (English + Japanese, built with MkDocs Material):

| Section | Contents |
|---|---|
| [Tutorials](docs/en/tutorials/01-installation.md) | Ordered learning path — start here |
| [How-to](docs/en/how-to/) | Task-oriented recipes (evals, permissions, sub-skills, …) |
| [Reference](docs/en/reference/) | CLI flags, DSL spec, Control IR op catalog |
| [Concepts](docs/en/concepts/) | Design principles P1–P8, workspace, events, permission model |

Build and serve locally:

```bash
make docs-install   # installs mkdocs + plugins
make docs-serve     # http://127.0.0.1:8000
```

---

## Project Status

Reyn is **pre-1.0 alpha**. The DSL, CLI, and event log are stable enough to build skills against; APIs may still change before 1.0. See [CLAUDE.md](CLAUDE.md) for architectural constraints and the testing policy.

---

## License

MIT — see [LICENSE](LICENSE).
