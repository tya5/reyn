# Reyn

**LLM workflow OS — predictable, auditable, constrained.** · 🏠 <https://tya5.github.io/reyn/>

[![CI](https://github.com/tya5/reyn/actions/workflows/test.yml/badge.svg)](https://github.com/tya5/reyn/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

```bash
git clone https://github.com/tya5/reyn.git
cd reyn && pip install -e ".[dev]"
reyn init
reyn run my_skill "Write a report on AI in education."
```

---

## Why Reyn

Most agent frameworks expose the act-sense-react loop as a programmable surface — the developer wires the graph, the LLM picks transitions, and keeping the loop coherent is the developer's problem. Reyn encodes the loop as a validated runtime contract. Four claims anchor 1.0:

- **Constrained-decision LLM (P3, P4).** The LLM picks only from an OS-provided candidate set: next phase + typed artifact + Control IR ops. It cannot invent a transition or bypass validation. Hallucinated phase names are rejected before any side effect.
- **Workspace + Events as single source of truth (P5, P6).** Every inter-phase value lives in the workspace; every state change emits an event into an append-only log. Crash recovery, replay, and audit derive from those primitives — not from in-memory state or application logs.
- **RAG framework foundation, not a finished RAG product.** Five primitive ops (`embed`, `index_write`, `index_query`, `recall`, `index_drop`) plus the `IndexBackend` protocol and the stdlib `index_docs` skill let you describe an indexing strategy as `skill.md` instead of a Python pipeline. Override the chunker per-source with a single python step. SQLite ships in 1.0; Qdrant / FAISS / Weaviate / Pinecone are post-1.0 plugin territory.
- **Dogfood-derived fix templates.** Two named, evidence-bound templates emerged from the pre-1.0 dogfood batches: a cognitive-bias callout (= named anti-attractor in instructions) and a multi-layer schema reinforcement (= system-prompt rule + tool-description rewrite together) for affordance-bias attractors. The latter restored a natural-concept-query scenario from 0/3 to 3/3 in one commit (batch 22).

The trade-off is explicit: predictability and auditability over maximum autonomy. If you want maximum LLM creative latitude with the densest ecosystem, LangGraph + LangChain will feel less restrictive.

---

## Quick Start

**Requirements:** Python 3.11+, a [LiteLLM](https://github.com/BerriAI/litellm)-compatible model endpoint.

```bash
pip install -e .               # local install; web UI: pip install -e ".[web]"
export OPENAI_API_KEY=sk-...   # or set the key for your LiteLLM proxy
reyn init                      # creates reyn.yaml + .reyn/config.yaml
```

> **A note on weak default models.** Reyn's default `models.standard`
> points at a low-cost LLM. With weak models, occasional empty replies
> on tool-heavy queries (= "list available skills" / "explain how X
> works") are normal — measured ~15% rate on `gemini-2.5-flash-lite`,
> dissolves on stronger models. Edit `reyn.yaml`'s `models.standard` to
> point at a stronger model if the rate matters for your use. Tracked
> as G12 in `docs/deep-dives/journal/dogfood/giveup-tracker.md`.

### Build a minimal skill

Create files under `reyn/local/my_skill/`:

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

Full tutorial: [docs/guide/getting-started/02-your-first-skill.md](docs/guide/getting-started/02-your-first-skill.md)

### Index documents and chat

```bash
# Index any glob into a named source — one source per chunking strategy.
# The CLI also accepts the bare data dict
#   '{"source":..., "path":..., "description":...}'
# and auto-wraps it with the artifact envelope.
reyn run index_docs '{"type":"index_docs_input","data":{"source":"my_docs","path":"docs/**/*.md","description":"Project documentation"}}'

# Chat — the LLM calls `recall` automatically when an indexed source covers the topic.
reyn chat
> What is the care boundary in Reyn?
```

The LLM picks the chunking strategy from a closed candidate set (P4); the chunk → embed → write chain runs deterministically in the skill postprocessor (no LLM, no attractor surface). To override the chunker for a specialised corpus (Python AST, SQL schemas, structured YAML), drop in a `skill.md` that extends `stdlib/index_docs` and swap one python step. Details and the full op surface in [docs/concepts/rag.md](docs/concepts/rag.md).

---

## How Reyn compares

| Framework | Loop enforcement | State persistence | Replay | Strength |
|---|---|---|---|---|
| **LangGraph** | Code-defined Python graph; conditional edges; LLM can pick arbitrary transitions when using `Command()` API | Checkpointer (SQLite / PostgreSQL) per super-step | Time-travel from any checkpoint | Expressiveness; LangChain ecosystem (600+ integrations) |
| **CrewAI** | Role-driven (sequential / hierarchical / Flow event-driven); no OS-level candidate constraint | Flow `@persist` (SQLite); manual resume on crash | Task replay (last run only) | Role-orchestration ergonomics; 30+ built-in tools; RAG and memory out of the box |
| **AutoGen** | Conversational multi-agent (message bus); LLM selects next speaker freely in SelectorGroupChat | `save_state()` / `load_state()` — application-managed, no built-in auto-checkpoint | OpenTelemetry spans (not replay-capable) | Multi-agent dialog patterns; actor model for distributed agents |
| **Semantic Kernel** | Function calling loop; LLM selects plugins autonomously; no OS-level candidate constraint | ChatHistory (in-memory); external DB persistence is app-managed | OpenTelemetry spans (not replay-capable) | Azure-native integration; C# / Python / Java parity; MIT OSS |
| **Reyn** | OS-enforced: validated transitions, closed candidate set (P3, P4) | Workspace + WAL, file-based SSoT (P5); automatic crash recovery | Append-only events log, replay-capable (P6) | Predictability; audit trail; weak-model viability; per-agent / per-chain / per-model cost caps; MCP server + client (bearer headers for hosted MCP); OAuth login + per-skill credential scoping (Confused Deputy mitigation); agent_id in P6 events (SOC2 / METI audit trail); RAG framework foundation (skill.md-driven indexing strategy override) |

**Reyn is more constrained.** If you want maximum LLM autonomy and creative agent behavior, LangGraph or AutoGen will feel less restrictive.

**Reyn ships a RAG framework foundation, not a mature RAG product.** The differentiator is that you write your indexing strategy as a `skill.md` — LLM-driven adaptive chunking with a deterministic postprocessor chain — not a Python pipeline. Override the chunker per-source by swapping a single python step. End-to-end smoke (= `reyn run index_docs` against `docs/concepts/*.md` → 418 chunks via real `gemini-embedding-001` → `reyn chat` with natural concept queries) returned indexed semantic answers in 3/3 runs (batch 22, 2026-05-10). Maturity gaps (rerank / HyDE / contextual retrieval / RAG eval framework / IDE integration / vector store variety beyond SQLite) live downstream — see [Project Status](#project-status) and [docs/concepts/rag.md](docs/concepts/rag.md).

**Reyn is smaller.** No chain abstractions, no rich vector store ecosystem — those live downstream (see [care-boundary.md](docs/concepts/care-boundary.md)).

**Reyn is opinionated about state.** The Workspace is the only inter-phase data channel; Events are the only audit log. Other frameworks let you pass state in-memory or through callbacks — convenient, but invisible to crash recovery and audit trails.

**Time-travel debugging.** Reyn ships a replay CLI that walks any past run step by step (`--mode replay`), and a compare CLI that diffs two runs side by side (`--mode compare`). See [docs/reference/dogfood-tracing.md](docs/reference/dogfood-tracing.md).

### Reyn fits when

- You need every LLM decision to be replayable and auditable (regulated, enterprise, or production environments where explainability matters).
- You want weak models to be reliable — the structural constraints (P4, P5) compensate for capability gaps without prompt-level workarounds.
- You need predictable cost: the closed candidate set prevents surprise tool invention and unbounded loops, and token + USD caps per agent / chain / model refuse-on-exceed to prevent runaway spend (see [docs/reference/config/budget.md](docs/reference/config/budget.md)).
- You want to integrate with Claude Code, Claude Desktop, Cursor, or any MCP-aware client — `reyn mcp serve` exposes your agent fleet via MCP. See [docs/reference/cli/mcp.md](docs/reference/cli/mcp.md).
- You need enterprise-grade credential handling: `reyn auth login <provider>` runs the RFC 8628 Device Authorization Grant flow; tokens auto-refresh silently; per-skill credential scoping prevents one skill from reaching another skill's secrets (Confused Deputy mitigation). Agent identity (`agent_id`) propagates through P6 events and MCP headers — ready for SOC2 / ISO27001 / METI v1.1 audit trails.

### Reyn does not fit when

- You want quick prototyping with maximum flexibility — LangGraph + LangChain's ecosystem is substantially denser.
- Your agent is single-shot and stateless — use a plain LLM call; the OS overhead is not worth it.
- You need a mature RAG product with rerank / HyDE / contextual retrieval / a RAG eval framework / IDE integration — Reyn ships a foundation (`index_docs` + `recall` + `IndexBackend` plugin path), not the mature ecosystem. LangChain / LlamaIndex have substantially denser RAG tooling.
- You want a UI or dashboard out of the box — that is downstream territory (see [care-boundary.md](docs/concepts/care-boundary.md#downstream-tooling--what-builds-on-reyn)).

**Further reading:**
[Why this design — principles](docs/concepts/principles.md) |
[What Reyn cares about — care boundary](docs/concepts/care-boundary.md) |
[Architecture, including the act-sense-react loop](docs/concepts/architecture.md)

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

Details: [docs/concepts/architecture.md](docs/concepts/architecture.md) | [docs/concepts/principles.md](docs/concepts/principles.md)

---

## Documentation

📖 **Read the docs online: <https://tya5.github.io/reyn/docs/>**

🏠 **Project landing page: <https://tya5.github.io/reyn/>**

Built with MkDocs Material; English + Japanese (translation in progress).

| Section | Live | Source |
|---|---|---|
| Tutorials | [tya5.github.io/reyn/docs/guide/getting-started/](https://tya5.github.io/reyn/docs/guide/getting-started/01-installation/) | [docs/guide/getting-started/](docs/guide/getting-started/) |
| How-to | [/docs/guide/for-skill-authors/](https://tya5.github.io/reyn/docs/guide/for-skill-authors/validate-artifacts/) | [docs/guide/for-skill-authors/](docs/guide/for-skill-authors/) |
| Reference | [/docs/reference/](https://tya5.github.io/reyn/docs/reference/cli/run/) | [docs/reference/](docs/reference/) |
| Concepts | [/docs/concepts/](https://tya5.github.io/reyn/docs/concepts/principles/) | [docs/concepts/](docs/concepts/) |

Build and serve locally:

```bash
make docs-install   # installs mkdocs + plugins
make docs-serve     # http://127.0.0.1:8000
```

---

## Talk to Reyn from another LLM (MCP)

Reyn ships an MCP (Model Context Protocol) server, so an outer LLM — Claude Desktop, Claude Code, Cursor, OpenAI Agents SDK, anything that speaks MCP — can converse with a Reyn agent. Reyn already consumes external MCP servers; this lets external clients consume Reyn back.

### Wire it up (Claude Desktop example)

Two transports; pick whichever fits your setup.

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

The same `AgentRegistry` / `BudgetTracker` backs the browser UI and external MCP clients. With `--reload`, edits to Reyn's source restart the server in-place; MCP clients reconnect automatically.

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

`--project` goes in `args`, not a `cwd` field: Claude Desktop and similar MCP clients don't honour a `cwd` field in their server config, so the spawned process can land in `/`. Restart the MCP host fully (= Quit, not just close window) after changing the config in either case.

**Why two?** Stdio = no network, no port, simplest in air-gapped or headless setups. SSE = shared lifecycle with the web UI, hot reload during development, and no per-client subprocess.

### Tools exposed

```
reyn:list_agents()
    List currently-registered agents in the project.

reyn:send_to_agent(agent_name, message)
    Send one user message to the named agent. Returns the agent's
    final reply text. Multi-turn conversation accumulates because
    Reyn persists per-agent chat history across calls.
```

That's it for v1. Tool-call tracing / narration streaming and ask-user / sampling integration are tracked for future iterations.

---

## Talk to Reyn from another agent (A2A)

Where MCP exposes Reyn as a tool provider, **A2A (Agent2Agent)** exposes Reyn agents as addressable peers — for other agents (LangGraph, CrewAI, custom A2A speakers) to discover and converse with directly. Enabled out of the box on the same FastAPI gateway as the web UI and MCP-over-SSE — no extra process, no extra port.

```bash
reyn web --port 8080         # already running for the UI / MCP
```

### Discovery

```
GET http://localhost:8080/a2a/agents/<name>/.well-known/agent-card.json
GET http://localhost:8080/a2a/agents               # list all
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

The reply comes back as a standard A2A `Message` (`role: agent`, with text parts). Multi-turn history is preserved across calls — same backing as MCP.

### v1 supported / not yet

Supported: `message/send` (synchronous), Agent Card discovery, multi-turn history persistence.

Not yet (tracked as A2A v2 follow-ups): `message/stream`, task lifecycle (`tasks/get`, `tasks/cancel`, push notifications), authentication, non-text message parts.

Spec reference: <https://google.github.io/A2A/>.

---

## Read Reyn from the inside (agent navigation)

The Reyn agent can read **its own repository** to answer questions like "how does Reyn's chat router work?" or "show me the postprocessor implementation". Two router tools, always present, no permission setup required:

```
reyn_src_list(path)   # list entries under <reyn_root>/path
reyn_src_read(path)   # read text of <reyn_root>/path
```

`<reyn_root>` is the directory holding the running Reyn install's `pyproject.toml`. `path` is the same path you'd see on GitHub. Pass `""` to list the repo top level. Path traversal outside the root is refused; binaries and oversized files are refused. There is no permission gate — Reyn's own repo is public OSS content (= GitHub secret-scanning blocks credentials at push time).

When `recall` is available against an indexed source whose description covers Reyn (= the typical setup once you've run `reyn run index_docs '{"source":"reyn_concepts",...}'`), the chat agent prefers `recall` for "what is X?" / "explain X" / "how does X work?" questions and falls back to `reyn_src_read("README.md")` only when no indexed source matches. This routing is what batch 22's affordance-bias fix established.

### Top-level layout

| Path | What's there |
|---|---|
| `src/reyn/` | Python package (chat, kernel, op_runtime, permissions, schemas). |
| `src/stdlib/skills/` | Bundled stdlib skills (eval, skill_builder, chat_compactor, index_docs, …). |
| `docs/` | Diátaxis docs site source — concepts, how-to, reference, ADRs. |
| `cookbook/` | Example skills and configurations. |
| `tests/` | Tier 1/2/3 tests. See `docs/deep-dives/contributing/testing.md`. |
| `pyproject.toml` | Package metadata + tool config (ruff, pytest). |

### Recommended deep-dive entry points

**Architectural questions** ("what is Reyn?"):
`docs/concepts/architecture.md`, `docs/concepts/principles.md` (P1–P8), `docs/concepts/phase-vs-skill-vs-os.md`, `docs/concepts/rag.md`, `CLAUDE.md`.

**Implementation questions** ("how does X work?"):
`src/reyn/chat/router_loop.py`, `src/reyn/chat/router_system_prompt.py` (= recall vs reyn_src_read routing), `src/reyn/chat/session.py`, `src/reyn/kernel/runtime.py`, `src/reyn/kernel/control_ir_executor.py`, `src/reyn/op_runtime/` (file, web, mcp, ask_user, embed, index_*, recall, …), `src/reyn/permissions/permissions.py`.

**Design rationale**: `docs/deep-dives/decisions/` — ADRs. Recent: ADR-0033 (RAG framework foundation), ADR-0030 (universal secret handling), ADR-0026 (unified tool registry). FP-0016 proposal: `docs/deep-dives/proposals/0016-agent-authentication.md` (all five components landed 2026-05-11 – 2026-05-16).

**Testing / contribution**: `docs/deep-dives/contributing/testing.md` (Tier 1/2/3 policy), `docs/deep-dives/contributing/dogfood-discipline.md` (cognitive-bias and affordance-bias fix templates).

When in doubt: `reyn_src_list("")` to see the top, `reyn_src_read(<path>)` to dive in.

---

## Project Status

**1.0 OSS launch ready.** The release-blocker schema-layer fix landed in batch 22; the framework foundation is green; the dogfood discipline is operational. As of 2026-05-16:

- Test suite: **3007 collected** on `main`.
- Recent main HEAD: `45c7035` — FP-0016 authentication stack (MCP bearer headers + OAuth refresh lifecycle + `reyn auth login` device grant CLI + per-skill credential scoping + agent_id propagation) + FP-0034 wrapper-only universal action catalog (N=5 production-grade, batch 26) + RAG framework foundation (ADR-0033 Phase 1 Accepted) + Flywheel milestone.
- Stable surfaces: DSL (skill.md / phase.md / artifact YAML), CLI (`reyn run` / `reyn chat` / `reyn web` / `reyn mcp serve` / `reyn source` / `reyn auth`), event log envelope (`ts`, `kind`, `phase`, `run_id`, `agent_id`, payload).
- Dogfood evidence: batch 22 restored a natural-concept-query scenario (Q1–Q3 against indexed `docs/concepts/*.md`) from 0/3 to 3/3 in one commit by combining a system-prompt routing rule with two tool-description rewrites — see `docs/deep-dives/journal/dogfood/2026-05-10-batch-22-affordance-bias-fix/findings.md`. FP-0034 wrapper-only universal action catalog reached N=5 production-grade stability (batch 26).

### What's not in 1.0 (= maturity gaps, deliberate)

The "framework foundation" framing is honest, not a hedge. The following live downstream:

- **Vector store plugin variety beyond SQLite.** Qdrant / FAISS / Weaviate / Pinecone via the `IndexBackend` protocol — phase 2 (post-1.1).
- **Advanced retrieval.** Rerank / HyDE / contextual retrieval / hierarchical — phase 2.
- **RAG evaluation framework.** Hosted eval pipelines and rubric marketplaces — eval-as-a-service is a downstream consumer of `LLMReplay`, not bundled.
- **IDE integration.** Editor plugins, in-IDE retrieval, semantic search panels — downstream territory.
- **Memory layer migration.** Reyn 0.x's inline-expansion memory continues to work; migration to `recall(sources=["memory"])` is phase 1.5 (1.1+), gated on dogfood-retest non-regression.
- **Local embedding models.** sentence-transformers / ollama — phase 2.
- **Sensitive-data redaction policy.** Phase 2 — in 1.0, the docs warn but do not redact.

Post-1.0 vision (= the "Flywheel" — operational intelligence, skill self-improvement, RAG routing) is captured under `docs/deep-dives/research/landscape/milestone-flywheel.md` and FP-0006..0010. Mentioned for transparency, not promised.

See [CLAUDE.md](CLAUDE.md) for architectural constraints and the testing policy.

---

## License

MIT — see [LICENSE](LICENSE).

---

## Acknowledgements — powered by AI

Reyn is **powered by AI** in two senses:

1. **At runtime.** Reyn is an LLM workflow OS. Every Skill execution delegates decisions to an LLM provider via LiteLLM. Without a capable model behind it, Reyn is just a runtime with nothing to run.
2. **In its development.** Substantial portions of this codebase, the stdlib skills, the documentation, the landing page, and the TUI were drafted with AI tooling — primarily Claude Code (Anthropic) for implementation and Claude Design for the website. Human review, integration, and the final architectural calls live with the maintainer; the AI contributions are recorded as `Co-Authored-By: Claude ...` trailers throughout the git history.

This disclosure is mandatory rather than promotional. If you're auditing the project's provenance — for security review, license compliance, or simply to understand the AI-assistance posture — start with `git log --grep="Co-Authored-By: Claude"` and the design prompts under `website/_design/`.
