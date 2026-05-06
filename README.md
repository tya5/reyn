# Reyn

**LLM workflow OS — predictable, auditable, constrained.** · 🏠 <https://tya5.github.io/reyn/>

[![CI](https://github.com/tya5/reyn/actions/workflows/test.yml/badge.svg)](https://github.com/tya5/reyn/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

```bash
# Reyn is alpha (0.1.x) — install from source for now; PyPI release follows 1.0.
git clone https://github.com/tya5/reyn.git
cd reyn && pip install -e ".[dev]"
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

📖 **Read the docs online: <https://tya5.github.io/reyn/docs/>**

🏠 **Project landing page: <https://tya5.github.io/reyn/>**

Built with MkDocs Material; English + Japanese (translation in progress).

| Section | Live | Source |
|---|---|---|
| Tutorials | [tya5.github.io/reyn/docs/tutorials/](https://tya5.github.io/reyn/docs/tutorials/01-installation/) | [docs/en/tutorials/](docs/en/tutorials/) |
| How-to | [/docs/how-to/](https://tya5.github.io/reyn/docs/how-to/validate-artifacts/) | [docs/en/how-to/](docs/en/how-to/) |
| Reference | [/docs/reference/](https://tya5.github.io/reyn/docs/reference/cli/run/) | [docs/en/reference/](docs/en/reference/) |
| Concepts | [/docs/concepts/](https://tya5.github.io/reyn/docs/concepts/principles/) | [docs/en/concepts/](docs/en/concepts/) |

Build and serve locally:

```bash
make docs-install   # installs mkdocs + plugins
make docs-serve     # http://127.0.0.1:8000
```

---

## Talk to Reyn from another LLM (MCP)

Reyn ships an MCP (Model Context Protocol) server, so an outer LLM —
Claude Desktop, Claude Code, Cursor, OpenAI Agents SDK, anything that
speaks MCP — can converse with a Reyn agent. This is the symmetric
counterpart of Reyn's existing MCP-client role: Reyn already _consumes_
external MCP servers; this lets external clients _consume_ Reyn back.

### Wire it up (Claude Desktop example)

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "reyn": {
      "command": "/absolute/path/to/your/python/bin/reyn",
      "args": [
        "mcp", "serve",
        "--project", "/absolute/path/to/your/reyn-project"
      ]
    }
  }
}
```

Why `--project` is in `args`, not a `cwd` field: Claude Desktop and
similar MCP clients don't honour a `cwd` field in their server config,
so the spawned process can land in `/`. Pass the project path as a CLI
argument instead.

Restart the MCP host fully (= Quit, not just close window).

### Tools exposed

```
reyn:list_agents()
    List currently-registered agents in the project.

reyn:send_to_agent(agent_name, message)
    Send one user message to the named agent. Returns the agent's
    final reply text. Multi-turn conversation accumulates because
    Reyn persists per-agent chat history across calls.
```

That's it for v1. Tool-call tracing / narration streaming and
ask-user / sampling integration are tracked for future iterations.

---

## Project Status

Reyn is **pre-1.0 alpha**. The DSL, CLI, and event log are stable enough to build skills against; APIs may still change before 1.0. See [CLAUDE.md](CLAUDE.md) for architectural constraints and the testing policy.

---

## License

MIT — see [LICENSE](LICENSE).

---

## Acknowledgements — powered by AI

Reyn is **powered by AI** in two senses, and the project tries to be
explicit about both:

1. **At runtime.** Reyn is an LLM workflow OS. Every Skill execution
   delegates decisions to an LLM provider via LiteLLM. Without a
   capable model behind it, Reyn is just a runtime with nothing to
   run.
2. **In its development.** Substantial portions of this codebase, the
   stdlib skills, the documentation, the landing page, and the TUI
   were drafted with AI tooling — primarily Claude Code (Anthropic) for
   implementation and Claude Design for the website. Human review,
   integration, and the final architectural calls live with the
   maintainer; the AI contributions are recorded as
   `Co-Authored-By: Claude ...` trailers throughout the git history.

This disclosure is mandatory rather than promotional. If you're
auditing the project's provenance — for security review, license
compliance, or simply to understand the AI-assistance posture — start
with `git log --grep="Co-Authored-By: Claude"` and the design prompts
under `website/_design/`.
