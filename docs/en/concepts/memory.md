---
type: concept
topic: architecture
audience: [human, agent]
---

# Memory

Memory is reyn's mechanism for facts that should outlive a single run: user preferences, project conventions, prior decisions, agent-specific habits. The router phase (`skill_router/classify`) reads memory on every chat turn and decides whether to write new entries.

There is no separate memory API in the OS — memory is just files plus the `file/read` and `file/write` ops the router phase uses through normal permission rules.

## Two layers

| Layer | Lives at | Visible to | Purpose |
|-------|----------|------------|---------|
| Shared | `.reyn/memory/` | All agents in the project | Project-wide facts: who the user is, project decisions, external references |
| Agent | `.reyn/agents/<name>/memory/` | Only that agent | Agent-specific behavior: a researcher's preferred sources, a writer's voice |

Both layers share the same shape: a `MEMORY.md` index plus one `<slug>.md` body file per entry. ChatSession reads both `MEMORY.md` files on every router turn, merges them into a single view, and embeds it in the routing artifact as `memory_index.content`. The merged view has two clearly-marked sections so the LLM can tell which layer an entry came from:

```markdown
# Memory Index (shared)

- [User Role](user_role.md) — backend engineer with 10y Python
- [Project Vision](project_reyn_vision.md) — predictability over autonomy

# Memory Index (agent: researcher)

- [Search Pref](feedback_arxiv_first.md) — prefers arxiv before web search
```

`(empty)` appears under either heading when a layer has no entries yet.

## Choosing a layer

The router prompt instructs the LLM to use **shared** when uncertain — broader visibility is the safer default:

- **Shared**: facts that benefit every agent (user role, project decisions, deadlines, external system pointers)
- **Agent**: facts only meaningful for *this* agent (its voice, its retrieval habits, behaviors that other agents shouldn't inherit)

When the LLM decides to save, it emits two ops in the same router turn:

1. A `file/write` for the body file at the chosen layer's path
2. A `file/regenerate_index` op so the layer's `MEMORY.md` picks up the change

The runtime rebuilds `MEMORY.md` mechanically from every body file's frontmatter — the LLM never writes `MEMORY.md` directly. This makes index correctness independent of model capability: a cheap model that historically dropped entries while reconstructing the index by hand can no longer do so.

Each layer has its own MEMORY.md on disk; the merged `(shared)` / `(agent)` headings only exist in the in-memory view ChatSession synthesizes for the LLM.

## Read path

```
ChatSession._invoke_router
  └─ _merge_memory_indexes(shared_path, agent_path, agent_name)
       ├─ reads .reyn/memory/MEMORY.md (if present)
       ├─ reads .reyn/agents/<name>/memory/MEMORY.md (if present)
       └─ returns {status, content}  ← embedded in chat_routing_request artifact

skill_router classify phase
  └─ LLM sees memory_index.content alongside user_message + history
```

If a description in the index is too vague to answer from, the LLM emits an `act` turn with `file/read` for the body file (`.reyn/memory/<slug>.md` for shared, `.reyn/agents/<chat_id>/memory/<slug>.md` for agent). The phase's permissions allow recursive reads under both `.reyn/memory` and `.reyn/agents`.

## Write path

The router phase has `file.write` permission for both layers. The LLM constructs paths from `chat_id` (= the agent's own name) and never writes into another agent's directory. There is no enforcement at the OS layer beyond the directory-prefix permission grant — the trust boundary is the LLM prompt, audited via the events log.

After every body-file write the LLM emits a `file/regenerate_index` op (PR19). The op is fully parameterized — `output_path`, `entry_template`, and `header` are supplied by the caller — so the OS file runtime stays format-agnostic (no `MEMORY.md` filename or em-dash entry format embedded in OS code, per P7). The same parameterized helper is used by `reyn memory edit` / `delete` / `import` to keep the on-disk index in sync after CLI mutations.

## Symmetry with docs

The relationship between memory and docs is intentional:

| Memory | Docs |
|--------|------|
| What the system has learned about *this* user/project | What the system *can do* in general |
| Read inline by `skill_router` | Read by `recall_docs` (planned, not yet implemented) |
| Persisted across runs | Static |

`recall_docs` is on the residual list — once shipped it will provide a project-documentation analogue with the same 2-tier shape but a different read trigger.

## Where memory differs from events

| | Memory | Events |
|---|--------|--------|
| Across-run state? | Yes | No (per-run, append-only audit) |
| Author | The user, via the router LLM persisting facts | The OS |
| Format | Markdown w/ frontmatter | JSONL |
| Read by | `skill_router` classify phase | `reyn events` CLI |

Events answer "what happened in this run?"; memory answers "what should I know going into the next run?"

## Staleness

Memory is a snapshot in time. A "feedback" entry from six months ago may no longer apply; a "project" entry that names a file path may be wrong if the file moved. The router LLM is instructed to verify before acting on specifics.

The system does not auto-decay or expire entries. Pruning is left to the user via `reyn memory delete` (which removes the body file and resyncs the index). PR19's mechanical regen makes a separate `gc` step unnecessary — the index can never drift from the on-disk body files.

## See also

- [Reference: skill_router](../reference/stdlib/skill_router.md) — the phase that reads/writes memory
- [Reference: profile-yaml](../reference/dsl/profile-yaml.md) — agent profile shape
- [Reference: state-dir](../reference/config/state-dir.md) — `memory/` and `agents/<name>/` locations
- [events.md](events.md)
