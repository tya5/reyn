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

## The categories (§D18 master taxonomy)

| Category | Holds | Canonical invoke semantic |
|---|---|---|
| `skill` | Project / stdlib skills | run the skill with `input` artifact |
| `agent.peer` | Peer agents in the topology | delegate a message to that peer |
| `mcp` | MCP server management + tool dispatch | six verb_object actions — see below |
| `file` | Workspace file ops | read / write / delete / list |
| `web` | Web search + fetch | search or fetch |
| `memory_entry` | Persistent memory records | read the entry's body |
| `memory_operation` | Memory CRUD ops | `remember_shared` / `remember_agent` / `forget` |
| `reyn_source` | Reyn source / docs (read-only) | read or list |
| `rag_corpus` | Indexed corpora (resource) | recall against this single source |
| `rag_operation` | RAG management ops | multi-source recall / drop source |
| `exec` | Sandboxed argv execution | run argv under the sandbox backend |

The `mcp` category provides six verb_object actions that cover the LLM-visible surface:

| Action | Purpose |
|---|---|
| `mcp__search_registry`  | Search the official MCP registry for new servers |
| `mcp__install_registry` | Install a server from the official MCP registry |
| `mcp__install_package`  | Install via a third-party package (npm/pypi/docker) or a GitHub repo URL |
| `mcp__install_local`    | Register a local command (e.g. LLM-authored script) as an MCP server |
| `mcp__list_servers`     | Enumerate installed servers |
| `mcp__list_tools`     | Enumerate one server's tools as `<server>__<tool>` ids |
| `mcp__call_tool`      | Call a tool by `<server>__<tool>` id with `args` |
| `mcp__drop_server`    | Remove an installed server |

`exec` is gated by `is_exec_available()` — it only appears when a real
sandbox backend (= not `"noop"`) is configured. The rest are always
visible.

## Qualified-name format

```
<category>__<entry_name>
```

The separator is **double underscore** (`__`). Categories may contain
dots (`agent.peer`, `rag_corpus`, `reyn_source`, …); entry names may
contain anything except the `__` sequence at the boundary. The split
rule is "first `__` after the category name" so `agent.peer__alice`
correctly parses as (`agent.peer`, `alice`).

Examples:

| Qualified name | Parses to |
|---|---|
| `skill__index_docs` | (`skill`, `index_docs`) |
| `agent.peer__alice` | (`agent.peer`, `alice`) |
| `mcp__call_tool` | (`mcp`, `call_tool`) |
| `mcp__install_registry` | (`mcp`, `install_registry`) |
| `rag_corpus__meetings` | (`rag_corpus`, `meetings`) |
| `file__read` | (`file`, `read`) |

### Provider portability — dots in qualified names

OpenAI's native function-call API restricts tool names to
`^[a-zA-Z0-9_-]{1,64}$` (= no `.`). Reyn's qualified names with
dotted categories (`agent.peer`, `rag_corpus`, `reyn_source`, etc.)
therefore **work via a LiteLLM proxy** but may be rejected by direct
OpenAI native callers.

Reyn's default setup routes all providers through LiteLLM
(`reyn.yaml: models: standard: openai/...`), so the dotted form works
end-to-end out of the box for the bundled scenarios. Gemini /
Anthropic / OpenAI-compatible endpoints all tolerate the `.` when
called via LiteLLM.

If you wire up a direct-OpenAI-native caller (= no LiteLLM in the
middle), you'll need either:

  - keep using a LiteLLM proxy in front (= recommended; matches the
    Reyn default), OR
  - migrate qualified names to use `_` everywhere (= breaking change
    across catalog enumerators / dispatch tables / hot-list / fixtures /
    scenarios; tracked at FP-0034 §D18 should it become real).

The migration is out of scope today because no direct-OpenAI-native
path exists in the Reyn project and the LiteLLM proxy is the canonical
ingress.

## The 3 wrappers

### `list_actions(category, filter, offset, limit) → {items, total}`

Browses the catalogue alphabetically. `category` is a list of category
names (omit or pass `[]` to include everything visible). `filter` is a
case-insensitive substring match against `qualified_name` and
`short_description`. `offset` / `limit` paginate. Items carry
`qualified_name` and a short description; long descriptions are
deliberately omitted so the listing stays compact.

In the **weak-model landing design**, a narrowed-category result instead
carries each item's full `description` and `input_schema` (the triple
`qualified_name` + `description` + `input_schema`), so the common flow is
`list_actions` → `invoke_action` with no intervening `describe_action`. See
[Weak-model discovery + selection reliability](#weak-model-discovery-selection-reliability).

### `describe_action(action_name) → {qualified_name, description, input_schema, metadata}`

Returns the long description, full input schema (= the underlying
tool's `parameters`), and metadata (`target_tool_name`, `category`,
`purity`) for one action. On an unknown name, returns a structured
error response per §D12 (see below).

Under the weak-model landing design, `describe_action` is **off the common
critical path** — `list_actions` already returns descriptions + schemas for
the narrowed category. It is retained for edge cases only: a single-name
lookup, or a category large enough that inlining every schema into the list
result would be wasteful. See
[Weak-model discovery + selection reliability](#weak-model-discovery-selection-reliability).

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
| `memory_entry` | read the body |
| `rag_corpus` | single-source recall |

The previous `mcp.server` / `mcp.tool` resource entries are removed; per-MCP-server / per-MCP-tool dispatch now flows through the verb actions in the `mcp` category (= `mcp__list_tools` →
`mcp__call_tool({tool: "<server>__<tool>", args})`).

This means an LLM can say `invoke_action("rag_corpus__meetings",
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
  memory_operation / reyn_source / rag_operation / mcp).
- **`_RESOURCE_RULES`** — category → `(target_tool_name,
  arg_transformer)` for resource categories whose entries come from
  `RouterCallerState` (skills / agents / memory entries / rag corpora).

Routing always:

1. Splits the qualified name into (`category`, `entry_name`).
2. Looks up the rule for that category / qualified name.
3. Runs the arg transformer (e.g. `_invoke_skill_args` wraps the
   caller args under the skill's input artifact).
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
system prompt gains a **`## Action categories`** section listing every
category with its canonical-default semantic. The section sits
between `## Capabilities` and `## Behaviour` so it stays inside the
static prompt-cache prefix (= every request after the first hits the
warm cache).

A Tier 2 invariant pins the section's bullet list to the `CATEGORIES`
tuple so future additions to the master taxonomy cannot drift from the
SP without the test failing.

## Weak-model discovery + selection reliability

The discover→invoke loop is only as good as the LLM's willingness to *use*
it. Strong models (`router_model: strong`) discover and select actions
flexibly from the category list and need no extra scaffolding. Weak / small
models (`router_model: light`) exhibit two reliable failure modes that the
catalog addresses **structurally**, so weak-model support never costs
strong-model flexibility:

1. **Satisficing** — the model invokes a visible hot-list action
   (`file__write`) instead of discovering a better-fit one (`file__edit`),
   because the hot action is "good enough".
2. **Discovery-skip** — the model does not proactively call `list_actions`;
   it guesses an action name from training priors, often malformed
   (`file.write`, `file__read_file`).

*Status: the no-names system prompt and the `file__edit` cross-reference are
shipped; `list_actions` returning schemas and the tier-gated mandates are the
agreed landing design (implementation in progress). Every lever below is
patch- and live-verified against `gemini-2.5-flash-lite` at reliable N.*

### No-names catalog

Action names appear in **exactly one place**: the `list_actions` result.
They are absent from the system prompt (which describes *categories* by
capability, never action names) and from every other tool's description.
This serves two ends:

- **Scalability** — the LLM-visible tool list and system prompt stay O(1)
  in the number of actions; a 200-action surface costs the same prompt as a
  20-action one.
- **Forced discovery of genuinely-unknown actions** — when a name exists
  nowhere the model could have memorised it, the only way to obtain it is to
  call `list_actions`. For genuinely-unknown actions this fires reliably
  (observed 16/16 `list_actions` for an obscure, non-guessable skill).

  Caveat — name-hiding forces discovery only for *unknown* actions. For
  training-**known** concepts (`file__read` / `file__write`) the weak model
  recalls the concept and emits a malformed approximation rather than
  discovering the exact name. Known-action *selection* is handled by the
  mechanical mandate below, not by name-hiding.

### `list_actions` returns name + description + schema

When `list_actions(category=[…])` narrows to a bounded set, each item carries
the **full triple** — `qualified_name`, `description`, and `input_schema`:

- **`description`** is what lets the model *select* the right action; a model
  cannot pick an action it cannot read (the conventional role of a tool
  description).
- **`input_schema`** is what lets the model *invoke* it with correct args.

Because the narrowed result carries both, the common flow is **two steps —
`list_actions` → `invoke_action`** — with no intervening `describe_action`.
Compactness is preserved by *category-narrowing* (schemas come only for the
category you asked about), not by omitting schemas globally.

Verified (schema → invocation axis): injecting schemas into the
`list_actions` result drove reactive `describe_action` calls 14→0 and
argument-correctness 0→12 (of 20) — with schemas in the list, the weak model
invokes correctly without a separate describe round-trip. The description →
selection axis is the conventional tool-description role (a model cannot
select an action it cannot read), so the description is carried on
design grounds rather than as a separately measured lever.

### Mechanical mandate (tier-gated)

Weak models **obey mechanical, unconditional procedural mandates** but
**ignore reasoning-based recommendations**. A cross-reference that *explains*
("for a partial edit, prefer `file__edit`") is ignored (0/20 followed it); an
unconditional mandate ("edits MUST use `file__edit`, NOT `file__write`") is
followed (edit 3 / write 1).

The router therefore gates a set of mechanical system-prompt mandates on the
model tier (`router_model: light` → on; `strong` → off):

- **`list_actions`-first** — the first tool call MUST be `list_actions`
  before reading, writing, or editing anything.
- **`file__edit`-MUST** — partial / surgical edits must use `file__edit`,
  not `file__write`.

Two properties make the mandate land:

1. **Explicit-action-enumeration wording.** Naming the concrete operations
   the mandate covers ("before reading, writing, or editing anything")
   produces 25-55% compliance; a generic phrasing ("before any other tool")
   produces 0-10%.
2. **Constraint reinforcement.** Repeating the mandate ~3× across the system
   prompt lifts compliance from ~36% to **~75-85%** (matched-pair verified,
   no distribution overlap). Repetition counters the goal-displacement that
   makes small models drop an instruction mid-reasoning.

### The ceiling

Explicit-enumeration wording + 3× reinforcement reaches **~75-85% weak-model
compliance** on the `list_actions`-first mandate. This is the practical
prompting ceiling: the residual ~15-25% is alignment fragility that prompting
alone does not close — narrowing it further would need fine-tuning, which is
out of scope. Strong models run with the mandates off and are unaffected.

### Unifying principle

> A weak model **self-discovers genuinely-unknown** actions and **obeys
> mechanical mandates**; it **recalls-and-flails on training-known** names and
> **ignores reasoning-based recommendations**. The catalog therefore hides
> names (forcing unknown-discovery), puts descriptions + schemas on the
> narrowed list (removing the describe round-trip), and gates mechanical
> mandates on the weak tier (fixing known-action selection) — while leaving
> strong models unconstrained.

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

## `embedding_class` default + graceful degrade (FP-0043 Phase 4)

Since **FP-0043 Phase 4**,
`ActionRetrievalConfig.embedding_class` defaults to `"local-mini"`
(= `sentence-transformers/all-MiniLM-L6-v2`). This makes
`search_actions` automatically available for any fresh installation
that runs `pip install 'reyn[local-embed]'` — no `reyn.yaml` edits
required.

When the `local-embed` extras are NOT installed, `ChatSession.__init__`
detects the missing import via a cheap `importlib.util.find_spec`
probe and silently treats the configured class as if it were `None`:
no `ActionEmbeddingIndex` is built, `search_actions` stays hidden by
the §D14 gate, and `list_actions` carries the hidden-state hint
pointing operators at the install command (= self-discoverable
mid-chat — see [Guide: enable semantic search](../../guide/for-users/enable-semantic-search.md)).

The probe lives in `src/reyn/chat/session.py` as
`_embedding_class_needs_missing_extras(class_name, embedding_config)`
and only returns `True` when:

1. The class's `model` string starts with `sentence-transformers/`,
2. `sentence_transformers` is **not** importable, AND
3. The configured class exists in `embedding.classes`.

Operators who prefer OpenAI-backed embeddings can override with
`action_retrieval.embedding_class: standard` (= or `light` / `strong`)
in `reyn.yaml`; setting it to `null` opts out of `search_actions`
entirely.

## What stays out of Phase 1

The structural surface is complete. Discovery features landed and
deferred:

**Landed post-1.0:**

- **`search_actions`** — semantic, embedding-backed search **shipped
  in FP-0043**. `ActionEmbeddingIndex` (= SQLite-WAL
  persistence + class-swap detection + cross-process build lock)
  backs the handler; visibility is gated by §D14 (= tool appears only
  once the index has built ≥1 vector). When the gate fails, the
  `list_actions` response carries a structured **hidden-state hint**
  pointing operators at `pip install 'reyn[local-embed]'` /
  `reyn embeddings status` so the install path is self-discoverable
  mid-chat. The local backend is the default; the OpenAI-backed
  classes (`light` / `standard` / `strong`) are equally usable. See
  [Guide: enable semantic search](../../guide/for-users/enable-semantic-search.md)
  and the [`reyn embeddings`](../../reference/cli/embeddings.md) CLI for
  the operator surface.

**Category validation + legacy redirect**

`list_actions(category=[...])` and `search_actions(category=[...])`
validate every supplied name against the live category enum.
Unknown names return an explicit error carrying a `legacy → current`
mapping (`mcp.server` → `mcp`, `agent.peer` → `multi_agent`, …) so
LLMs whose training data references a pre-collapse name self-correct
in a single retry. See `_LEGACY_CATEGORY_REDIRECTS` in
`src/reyn/tools/universal_catalog.py`.

**Deferred to Phase 2:**

- **`rag_corpus` enumeration** — needs a `RouterCallerState` field
  carrying indexed-source metadata, then plumbing through
  `RouterHostAdapter`. The `invoke` and `describe` paths already work
  for `rag_corpus__<name>` if the LLM knows the name.
- **`exec` enumeration** — needs sandbox-backend introspection. The
  visibility predicate exists; the catalogue body waits for the
  introspection API.
- **Hot-list** — `action_retrieval.hot_list_n` defaults to `0` (off)
  following N=0 viability measurements. `list_actions` is the canonical
  discovery path. Operators can opt in by setting `hot_list_n: 10+` in
  `reyn.yaml`; the seed, usage tracker, and alias-builder remain fully
  operative.

## Reference files

- [`src/reyn/tools/universal_catalog.py`](https://github.com/anthropics/reyn) — `CATEGORIES`, 4 ToolDefinitions, qualified-name parser, D14 helpers, real handlers
- [`src/reyn/tools/universal_dispatch.py`](https://github.com/anthropics/reyn) — routing tables, `ResolvedAction`, `UnknownActionError`, `suggest_similar_names`
- [`src/reyn/chat/router_tools.py`](https://github.com/anthropics/reyn) — `build_tools` integration (flag-gated wrappers)
- [`src/reyn/chat/router_system_prompt.py`](https://github.com/anthropics/reyn) — `## Action categories` section
- [`src/reyn/config.py`](https://github.com/anthropics/reyn) — `ActionRetrievalConfig`
- [`docs/reference/config/reyn-yaml.md`](../../reference/config/reyn-yaml.md#action_retrieval-block) — config reference
