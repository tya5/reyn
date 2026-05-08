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

> **A note on weak default models.** Reyn's default `models.standard`
> points at a low-cost LLM. With weak models, occasional empty replies
> on tool-heavy queries (= "list available skills" / "explain how X
> works") are normal — measured ~15% rate on `gemini-2.5-flash-lite`,
> dissolves on stronger models. If the rate matters for your use, edit
> `reyn.yaml`'s `models.standard` to point at a stronger model. This is
> tracked as G12 in `docs/deep-dives/journal/dogfood/giveup-tracker.md`.

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

Reyn supports two MCP transports; pick whichever fits your setup.

**Recommended — SSE (shared with the web UI, dev-loop friendly):**

```bash
reyn web --port 8080         # leave running in any terminal
# add `--reload` for the dev loop
```

Then in `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "reyn": {
      "transport": {"type": "sse", "url": "http://localhost:8080/mcp/sse"}
    }
  }
}
```

The same `AgentRegistry` / `BudgetTracker` backs the browser UI and
external MCP clients, so a `reyn web` already running for the UI
doubles as your MCP endpoint at no extra cost. With `--reload`,
edits to Reyn's source restart the server in-place; MCP clients
reconnect automatically — no Claude Desktop restart on each change.

**Fallback — stdio (subprocess, no port required):**

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

Restart the MCP host fully (= Quit, not just close window) after
changing the config in either case.

**Why two?** Stdio = no network, no port, simplest in air-gapped
or headless setups. SSE = shared lifecycle with the web UI, hot
reload during development, and no per-client subprocess. Pick stdio
when you need isolation, SSE when you're already running `reyn web`.

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

## Talk to Reyn from another agent (A2A)

Where MCP exposes Reyn to an outer LLM as a tool provider, **A2A
(Agent2Agent)** exposes Reyn agents as addressable peers — for other
agents (LangGraph, CrewAI, custom A2A speakers) to discover and
converse with directly.

A2A is enabled out of the box on the same FastAPI gateway as the web
UI and MCP-over-SSE — no extra process, no extra port:

```bash
reyn web --port 8080         # already running for the UI / MCP
```

### Discovery

Each Reyn agent is published at the canonical A2A discovery URL:

```
GET http://localhost:8080/a2a/agents/<name>/.well-known/agent-card.json
```

For convenience, you can list all Reyn agents on this server:

```
GET http://localhost:8080/a2a/agents
```

### Conversation (JSON-RPC 2.0)

Send a message to a Reyn agent:

```bash
curl -X POST http://localhost:8080/a2a/agents/default \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "message/send",
    "params": {
      "message": {
        "role": "user",
        "parts": [{"kind": "text", "text": "Hello"}]
      }
    }
  }'
```

The reply comes back as a standard A2A `Message` (`role: agent`, with
text parts). Multi-turn history is preserved across calls — same
backing as MCP.

### What's supported in v1

- `message/send` (synchronous, returns final reply)
- Agent Card discovery
- Multi-turn history persistence

### What's not (yet)

- `message/stream` (streaming SSE responses)
- Task lifecycle (`tasks/get`, `tasks/cancel`, push notifications)
- Authentication
- Non-text message parts (file / data)

These are tracked as A2A v2 follow-ups. Spec reference:
<https://google.github.io/A2A/>.

---

## Read Reyn from the inside (agent navigation)

The Reyn agent can read **its own repository** to answer questions like
"how does Reyn's chat router work?" or "show me the postprocessor
implementation". Two router tools, always present, no permission setup
required:

```
reyn_src_list(path)   # list entries under <reyn_root>/path
reyn_src_read(path)   # read text of <reyn_root>/path
```

`<reyn_root>` is the directory holding the running Reyn install's
`pyproject.toml` (= a clone or `pip install -e .` checkout). `path` is
the same path you'd see on GitHub. Pass `""` to list the repo top
level. Path traversal outside the root is refused; binaries and
oversized files are refused. There is **no permission gate** — Reyn's
own repo is public OSS content (= GitHub secret-scanning blocks
credentials at push time, so nothing in the tree is sensitive).

The chat agent's system prompt directs it to **start every "explain
Reyn" question from `reyn_src_read("README.md")`** — i.e. this
document. Below is the curated index it'll follow next.

### Top-level layout

| Path | What's there |
|---|---|
| `src/reyn/` | The Python package (= chat, kernel, op_runtime, permissions, schemas). |
| `src/stdlib/skills/` | Bundled stdlib skills (eval, skill_builder, chat_compactor, etc.). |
| `docs/` | Diátaxis docs site source — concepts, how-to, reference, ADRs. |
| `cookbook/` | Example skills and configurations. |
| `tests/` | Tier 1/2/3 tests. See `docs/en/contributing/testing.md` for the policy. |
| `pyproject.toml` | Package metadata + tool config (ruff, pytest). |

### Recommended deep-dive entry points

For "what is Reyn?" / architectural questions:

- `docs/en/concepts/architecture.md` — User → Agent → Skill → OS →
  Phase → Workspace overview.
- `docs/en/concepts/principles.md` — P1–P8 invariants (= the rules
  the OS enforces).
- `docs/en/concepts/phase-vs-skill-vs-os.md` — boundary between the
  three layers.
- `CLAUDE.md` — Tier 1 hard rules for code-writing agents.

For "how does X work?" / implementation questions:

- `src/reyn/chat/router_loop.py` — chat router (= what the user is
  talking to right now). Tool catalog, dispatch, empty-stop handling.
- `src/reyn/chat/router_system_prompt.py` — how the system prompt
  is assembled.
- `src/reyn/chat/session.py` — `ChatSession` (= the per-agent
  state, history, intervention queue).
- `src/reyn/kernel/runtime.py` — phase execution loop.
- `src/reyn/kernel/control_ir_executor.py` — Control IR dispatch.
- `src/reyn/op_runtime/` — op handlers (file, web, mcp, ask_user, …).
- `src/reyn/permissions/permissions.py` — permission model.

For design rationale:

- `docs/en/decisions/` — 23 ADRs. Each one explains a specific
  decision and the alternatives considered.

For testing / contribution:

- `docs/en/contributing/testing.md` — Tier 1/2/3 test policy.
- `docs/en/contributing/dogfood-discipline.md` — how Reyn was
  iterated via its own dogfood batches.

When in doubt: `reyn_src_list("")` to see the top, `reyn_src_list("docs/en")`
to browse the docs tree, and `reyn_src_read(<path>)` to dive in.

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
