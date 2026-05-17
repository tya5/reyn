---
type: concept
topic: universal-catalog
audience: [human, agent]
---

# Universal Action Catalog

A Reyn agent's chat router historically exposed a separate tool for each
discovery surface — `list_skills` / `list_mcp_tools` / `list_memory` /
`list_agents` / … — plus a separate `invoke_*` per kind. As the catalogue
grew, the LLM-facing tool list grew linearly: each new resource kind cost
the LLM a fresh tool to learn.

The **universal action catalog** (FP-0034) replaces N per-kind discover /
describe / invoke tools with **4 wrappers that cover every category
uniformly**. Every action — a skill, a peer agent, an MCP tool, a memory
entry, a file op, an indexed corpus, … — is addressed by a single
qualified name (`<category>__<entry>`) and dispatched through
`invoke_action`. Discovery happens through `list_actions` and detail
introspection through `describe_action`; semantic / natural-language
discovery uses `search_actions` (embedding-backed).

Since Phase 6 (2026-05-16), the wrapper-only path is the **sole
production behaviour**: legacy per-kind tools no longer appear in the
LLM-visible `tools=`. The handlers (`invoke_skill` /
`delegate_to_agent` / `call_mcp_tool` / …) remain in the registry as
**backing implementations** of the universal wrappers — `invoke_action`
dispatches to them through `universal_dispatch.py`. Validation:
dogfood batch 26 N=5 stability (= 32/35 = 91.4% verified, Brier 0.177,
hallucination 0/35).

## Why a single catalog

| Per-kind catalog (legacy) | Universal catalog (FP-0034) |
|---|---|
| N discover tools, one per resource kind | 1 `list_actions(category=[…])` |
| N describe tools, one per resource kind | 1 `describe_action(action_name)` |
| N invoke tools, one per resource kind | 1 `invoke_action(action_name, args)` |
| LLM tool list grows linearly with surface | LLM tool list is constant |
| Adding a new resource kind needs a new tool | Adding a kind needs a new category + dispatch rule |
| Each tool re-describes the same discover→describe→invoke pattern | One pattern documented once |

The architectural win is that **the LLM's tool list is now O(1) in
resource categories**. A 14th category does not add a 14th tool — it
adds an entry to the `CATEGORIES` tuple and one routing rule.

## The 13 categories (§D18 master taxonomy)

| Category | Holds | Canonical invoke semantic |
|---|---|---|
| `skill` | Project / stdlib skills | run the skill with `input` artifact |
| `agent.peer` | Peer agents in the topology | delegate a message to that peer |
| `mcp.server` | Configured MCP servers (resource) | list this server's tools |
| `mcp.tool` | Individual tools on each server | call the tool with `args` |
| `mcp.operation` | MCP server management ops | run the op (e.g. `drop_server`) |
| `file` | Workspace file ops | read / write / delete / list |
| `web` | Web search + fetch | search or fetch |
| `memory.entry` | Persistent memory records | read the entry's body |
| `memory.operation` | Memory CRUD ops | `remember_shared` / `remember_agent` / `forget` |
| `reyn.source` | Reyn source / docs (read-only) | read or list |
| `rag.corpus` | Indexed corpora (resource) | recall against this single source |
| `rag.operation` | RAG management ops | multi-source recall / drop source |
| `exec` | Sandboxed argv execution | run argv under the sandbox backend |

`exec` is gated by `is_exec_available()` — it only appears when a real
sandbox backend (= not `"noop"`) is configured. The rest are always
visible.

## Qualified-name format

```
<category>__<entry_name>
```

The separator is **double underscore** (`__`). Categories may contain
dots (`mcp.tool`); entry names may contain anything except the `__`
sequence at the boundary. The split rule is "first `__` after the
category name" so `mcp.tool__brave.search` correctly parses as
(`mcp.tool`, `brave.search`).

Examples:

| Qualified name | Parses to |
|---|---|
| `skill__index_docs` | (`skill`, `index_docs`) |
| `agent.peer__alice` | (`agent.peer`, `alice`) |
| `mcp.tool__brave.search` | (`mcp.tool`, `brave.search`) |
| `mcp.operation__drop_server` | (`mcp.operation`, `drop_server`) |
| `rag.corpus__meetings` | (`rag.corpus`, `meetings`) |
| `file__read` | (`file`, `read`) |

## The 3 wrappers

### `list_actions(category, filter, offset, limit) → {items, total}`

Browses the catalogue alphabetically. `category` is a list of category
names (omit or pass `[]` to include everything visible). `filter` is a
case-insensitive substring match against `qualified_name` and
`short_description`. `offset` / `limit` paginate. Items carry
`qualified_name` and a short description; long descriptions are
deliberately omitted so the listing stays compact.

### `describe_action(action_name) → {qualified_name, description, input_schema, metadata}`

Returns the long description, full input schema (= the underlying
tool's `parameters`), and metadata (`target_tool_name`, `category`,
`purity`) for one action. On an unknown name, returns a structured
error response per §D12 (see below).

### `invoke_action(action_name, args) → <target's result>`

Dispatches to the underlying tool via the routing layer (see
[Dispatch](#dispatch-routing-layer)). The wrapper is transparent: the
target handler runs with the full `ToolContext`, so permission gates,
events, budgets, and workspace effects behave exactly as if the legacy
tool had been called directly. On an unknown name, returns a §D12
error response.

A fourth wrapper, `search_actions`, is reserved for semantic
(embedding-backed) search. It is **not visible in Phase 1** — the
handler is a stub, the embedding plumbing waits for Phase 2.

## Canonical-default semantic (§D19)

Resource categories support invoke as well as discover. Invoking a
resource runs the *canonical default operation* for that kind:

| Resource category | Canonical default invoke |
|---|---|
| `skill` | run the skill |
| `agent.peer` | delegate a message |
| `mcp.server` | list this server's tools |
| `mcp.tool` | call the tool |
| `memory.entry` | read the body |
| `rag.corpus` | single-source recall |

This means an LLM can say `invoke_action("rag.corpus__meetings",
{"query": "Q3 roadmap"})` and the wrapper expands it to
`recall(sources=["meetings"], query="Q3 roadmap")` without the LLM
needing to remember the underlying call shape. The canonical default
is documented in `describe_action`'s response.

## Dispatch (routing layer)

The qualified name → target tool name mapping lives in
[`src/reyn/tools/universal_dispatch.py`](https://github.com/anthropics/reyn).
It is **pure** — no I/O, no state, no live invocation. Two tables drive
the routing:

- **`_OPERATION_RULES`** — qualified name → `(target_tool_name,
  arg_transformer)` for static operation categories (file / web /
  memory.operation / reyn.source / rag.operation / mcp.operation).
- **`_RESOURCE_RULES`** — category → `(target_tool_name,
  arg_transformer)` for resource categories whose entries come from
  `RouterCallerState` (skills / agents / mcp servers / mcp tools /
  memory entries / rag corpora).

Routing always:

1. Splits the qualified name into (`category`, `entry_name`).
2. Looks up the rule for that category / qualified name.
3. Runs the arg transformer (e.g. `_call_mcp_tool_args` splits the
   `entry_name` into `(server, tool)` and packs `args`).
4. Returns a `ResolvedAction(target_tool_name, target_args)` that
   the wrapper hands to the unified `ToolRegistry`.

If no rule matches, dispatch raises `UnknownActionError` carrying
`difflib`-ranked suggestions from the known qualified-name set + any
visible resource entries.

## Error response (§D12)

When `invoke_action` or `describe_action` receives an unknown
`action_name`, the response is structured rather than raised:

```json
{
  "error": "Unknown action 'skil__foo'",
  "reason": "...",
  "suggestions": ["skill__foo", "skill__form"],
  "hint": "Use list_actions(category=[...]) to discover the correct name."
}
```

`suggestions` come from `difflib.get_close_matches` against the
static qualified-name set merged with router-state-aware candidates.
The hint always points back at `list_actions` so the LLM has an
obvious recovery move.

## Visibility gating (§D14)

Some categories are visibility-gated by the runtime environment:

| Predicate | Effect |
|---|---|
| `is_search_available(embedding_class)` | Whether `search_actions` appears in tools= (Phase 2) |
| `is_exec_available(sandbox_backend)` | Whether `exec` appears in `list_actions` enumeration |

The gates are pure functions; the runtime supplies the configuration
values from `action_retrieval.embedding_class` and the resolved
sandbox backend. Hidden categories appear neither in the
`list_actions` `category=` enum nor in any enumeration result.

## System prompt placement (§D9)

When `action_retrieval.universal_wrappers_enabled` is true, the router
system prompt gains a **`## Action categories`** section listing all
13 categories with their canonical-default semantic. The section sits
between `## Capabilities` and `## Behaviour` so it stays inside the
static prompt-cache prefix (= every request after the first hits the
warm cache).

A Tier 2 invariant pins the section's bullet list to the `CATEGORIES`
tuple so future additions to the master taxonomy cannot drift from the
SP without the test failing.

## Default-on (PR-3b-iv)

`ActionRetrievalConfig.universal_wrappers_enabled` defaults to `True`
in production. Direct callers of `build_tools` or `build_system_prompt`
that don't pass an `ActionRetrievalConfig` (e.g. unit-test fixtures
constructing a `FakeRouterHost`) keep the legacy off behavior because
`RouterLoop` reads the flag through a `getattr(host,
"get_universal_wrappers_enabled", None)` fallback that returns
`False` when the method is missing. The dual path keeps LLMReplay
fixtures byte-valid while production routers get the new tools.

To opt out, add the following to `reyn.yaml`:

```yaml
action_retrieval:
  universal_wrappers_enabled: false
```

## What stays out of Phase 1

The structural surface is complete; behavioral / discovery features
deferred to Phase 2:

- **`search_actions`** — semantic, embedding-backed search. The
  handler is a stub; visibility waits for `ActionEmbeddingIndex`.
- **`rag.corpus` enumeration** — needs a `RouterCallerState` field
  carrying indexed-source metadata, then plumbing through
  `RouterHostAdapter`. The `invoke` and `describe` paths already work
  for `rag.corpus__<name>` if the LLM knows the name.
- **`exec` enumeration** — needs sandbox-backend introspection. The
  visibility predicate exists; the catalogue body waits for the
  introspection API.
- **Hot-list pinning** — `action_retrieval.hot_list_n` is parsed but
  unused; Phase 2 uses it to bias `list_actions` ordering toward the
  most recently invoked actions.

## Reference files

- [`src/reyn/tools/universal_catalog.py`](https://github.com/anthropics/reyn) — `CATEGORIES`, 4 ToolDefinitions, qualified-name parser, D14 helpers, real handlers
- [`src/reyn/tools/universal_dispatch.py`](https://github.com/anthropics/reyn) — routing tables, `ResolvedAction`, `UnknownActionError`, `suggest_similar_names`
- [`src/reyn/chat/router_tools.py`](https://github.com/anthropics/reyn) — `build_tools` integration (flag-gated wrappers)
- [`src/reyn/chat/router_system_prompt.py`](https://github.com/anthropics/reyn) — `## Action categories` section
- [`src/reyn/config.py`](https://github.com/anthropics/reyn) — `ActionRetrievalConfig`
- [`docs/reference/config/reyn-yaml.md`](../reference/config/reyn-yaml.md#action_retrieval-block) — config reference
