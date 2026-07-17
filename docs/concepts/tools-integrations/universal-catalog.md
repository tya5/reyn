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
uniformly**. Every action — run a workflow, delegate to a peer agent, call an
MCP tool, read a memory, search an indexed corpus, … — is addressed by a
single qualified name (`<category>__<entry>`) and dispatched through
`invoke_action`. Discovery happens through `list_actions` and detail
introspection through `describe_action`; semantic / natural-language
discovery uses `search_actions` (embedding-backed).

**Status update: tool presentation is now a pluggable scheme, not
a single fixed path.** Since Phase 6 (2026-05-16) the wrapper-only path was
briefly the sole production behaviour, but an owner-driven H1 fix later flipped
the `chat` layer's own default to `enumerate-all` — a flat, no-wrapper tool
list — because flat listing stops `invoke_action` name-hallucination (30%→100%
non-hot-list tool-use accuracy). `universal-category` (this page's wrapper path)
remains a registered scheme, reachable when an operator sets `tool_use.chat:
universal-category` in `reyn.yaml`. See [Tool-Use Schemes](tool-use-schemes.md)
for the full, current model — the sections below describe the
`universal-category` scheme's own mechanics, not which layer uses it by default.
(#2768 removed the dead phase-graph-era `step`/`phase` tool-use layers.)

When this wrapper path is active for a layer, the handlers (`invoke_skill` /
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
| `skill` | Project / stdlib workflows | run the workflow with `input` artifact |
| `agent.peer` | Peer agents in the topology | delegate a message to that peer |
| `mcp` | MCP server management + tool dispatch | six verb_object actions — see below |
| `file` | Workspace file ops | read / write / delete / list |
| `web` | Web search + fetch | search or fetch |
| `memory_operation` | Memory ops | `list` / `read` (by `layer` + `slug`) / `remember_shared` / `remember_agent` / `forget` |
| `reyn_repo` | Reyn source / docs (read-only) | read or list |
| `rag_operation` | RAG ops | `list_sources` / multi-source `semantic_search` / drop source |
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
| `mcp__call_tool`      | Call a tool by `<server>__<tool>` id with `tool_args` |
| `mcp__drop_server`    | Remove an installed server |

`exec` is gated by `is_exec_available()` — it only appears when a real
sandbox backend (= not `"noop"`) is configured. The rest are always
visible.

**Every category enumerates a fixed set of verbs.** A resource — a stored
memory, an indexed corpus, an installed MCP tool, a registered pipeline — is an
**argument** to a verb, never an enumerated action of its own, so the number of
actions the LLM is shown does not grow with what the operator has accumulated.
Where collapsing a resource category removed the only surface that *named* those
resources, a constant-count discovery verb replaces it (`memory_operation__list`,
`rag_operation__list_sources`, `mcp__list_tools`, `pipeline__list`,
`skill_management__list`).

## Qualified-name format

```
<category>__<entry_name>
```

The separator is **double underscore** (`__`). Categories may contain
dots (`agent.peer`, …); entry names may
contain anything except the `__` sequence at the boundary. The split
rule is "first `__` after the category name" so `agent.peer__alice`
correctly parses as (`agent.peer`, `alice`).

Examples:

| Qualified name | Parses to |
|---|---|
| `agent.peer__alice` | (`agent.peer`, `alice`) |
| `mcp__call_tool` | (`mcp`, `call_tool`) |
| `mcp__install_registry` | (`mcp`, `install_registry`) |
| `rag_operation__semantic_search` | (`rag_operation`, `semantic_search`) |
| `file__read` | (`file`, `read`) |

### Provider portability — dots in qualified names

OpenAI's native function-call API restricts tool names to
`^[a-zA-Z0-9_-]{1,64}$` (= no `.`). Reyn's qualified names with
dotted categories (`agent.peer`, etc.)
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

## Resource names: enumeration vs resolution (§D19)

**Enumeration** is what the LLM is *shown*; **resolution** is what a name a
caller already typed *does*. The two are deliberately different surfaces, and
only enumeration governs payload size.

No resource is enumerated. To reach one, discover it with the category's
discovery verb, then pass its id as an argument:

| To … | Discover with | Then invoke |
|---|---|---|
| search an indexed corpus | `rag_operation__list_sources` | `rag_operation__semantic_search({sources: ["meetings"], query: "Q3 roadmap"})` |
| read a stored memory | `memory_operation__list` | `memory_operation__read({layer: "shared", slug: "..."})` |
| call an MCP tool | `mcp__list_tools` | `mcp__call_tool({tool: "<server>__<tool>", tool_args})` |
| run a registered pipeline | `pipeline__list` | `pipeline__run({name: "greet", input: {...}})` |

`memory_operation__read` takes an explicit `layer` (`shared` or `agent`), so
both memory layers are reachable through the catalog.

Two **author-time** resource forms still **resolve** even though they are not
enumerated, because a human or an agent writes them by hand: `pipeline__<name>`
(the form the [pipeline guide](../../guide/for-users/write-a-pipeline.md)
teaches — `pipeline__greet({name: "Reyn"})`) and `mcp__<server>__<tool>` (a
`tool: mcp__echo__ping` step in a pipeline DSL file). Each reaches the same
target with the same effective args as its verb counterpart; resolving a name
the caller already typed costs zero tools, whereas enumerating one costs a tool
per resource.

## Dispatch (routing layer)

The qualified name → target tool name mapping lives in
[`src/reyn/tools/universal_dispatch.py`](https://github.com/anthropics/reyn).
It is **pure** — no I/O, no state, no live invocation. Two tables drive
the routing:

- **`_OPERATION_RULES`** — a **closed table of full literal qualified names** →
  `(target_tool_name, arg_transformer)`, covering every category. This is the
  enumerated surface: it is the only table an enumerator may read, which is what
  keeps the payload constant.
- **`_RESOURCE_RULES`** — category → `(target_tool_name, arg_transformer)`,
  consulted only when the full name is absent from `_OPERATION_RULES`. It holds
  the two author-time forms (`pipeline` / `mcp`) described above, and is
  **never** read by an enumerator.

Routing always:

1. Splits the qualified name into (`category`, `entry_name`).
2. Looks up the full qualified name in `_OPERATION_RULES`, falling back to
   the category's `_RESOURCE_RULES` entry.
3. Runs the arg transformer (e.g. `_mcp_tool_args` rewraps
   `mcp__echo__ping({...})` as `mcp_call_tool({tool, tool_args})`).
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
  (observed 16/16 `list_actions` for an obscure, non-guessable workflow).

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

**This section describes the underlying `universal_wrappers_enabled` flag's
own default, not which tool-use scheme resolves to it today** — see the status
update at the top of this page: the `tool_use.chat` scheme selector generalizes
this flag's *selection* role, and `chat`'s own scheme default (`enumerate-all`)
does not route through this flag at all. The flag itself remains live for the
`universal-category` scheme (catalog-wrapper vs direct-tool presentation).

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

## `embedding_class` default + graceful degrade

**FP-0043 Phase 4** defaulted `ActionRetrievalConfig.embedding_class` to
`"local-mini"` (= `sentence-transformers/all-MiniLM-L6-v2`), making
`search_actions` automatically available for any fresh installation
that ran `pip install 'reyn[local-embed]'` — no `reyn.yaml` edits
required. The **semantic-search-opt-in fix** reverted this: a truthy
default made reyn attempt a Hugging Face model download at chat
startup even on zero-config / offline installs, surfacing as a
startup warning when the download failed — contradicting the
project's standing principle that semantic search is opt-in.

`ActionRetrievalConfig.embedding_class` now defaults to `None` (off).
With no class configured, no embedding index build is attempted at
all — `search_actions` is simply absent from `tools=` per the §D14
gate below, silently (there is nothing to fail or warn about).
Operators opt in explicitly via `action_retrieval.embedding_class:
local-mini` (local model, needs the `reyn[local-embed]` extras) or
`standard` (API-backed, no local download) in `reyn.yaml` — see
[Guide: enable semantic search](../../guide/for-users/enable-semantic-search.md).

When an operator opts into an ST-backed class but the `local-embed`
extras are NOT installed, `Session.__init__`
detects the missing import via a cheap `importlib.util.find_spec`
probe and silently treats the configured class as if it were `None`:
no `ActionEmbeddingIndex` is built, `search_actions` stays hidden by
the §D14 gate, and `list_actions` carries the hidden-state hint
pointing operators at the install command (= self-discoverable
mid-chat — see [Guide: enable semantic search](../../guide/for-users/enable-semantic-search.md)).

The probe lives in `src/reyn/runtime/session.py` as
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
  mid-chat. Off by default (opt-in only, per the semantic-search-opt-in
  fix); once opted in, the local backend and the OpenAI-backed
  classes (`light` / `standard` / `strong`) are equally usable. See
  [Guide: enable semantic search](../../guide/for-users/enable-semantic-search.md)
  and the [`reyn embeddings`](../../reference/cli/embeddings.md) CLI for
  the operator surface.

**Category validation + legacy redirect**

`list_actions(category=[...])` and `search_actions(category=[...])`
validate every supplied name against the live category enum.
Unknown names return an explicit error carrying a `legacy → current`
mapping (`mcp.server` → `mcp`, `agent.peer` → `multi_agent`,
`memory_entry` → `memory_operation`, `rag_corpus` → `rag_operation`) so
LLMs whose training data references a pre-collapse name self-correct
in a single retry. See `_LEGACY_CATEGORY_REDIRECTS` in
`src/reyn/tools/universal_catalog.py`.

**Deferred to Phase 2:**

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
- [`src/reyn/runtime/router_tools.py`](https://github.com/anthropics/reyn) — `build_tools` integration (flag-gated wrappers)
- [`src/reyn/runtime/router_system_prompt.py`](https://github.com/anthropics/reyn) — `## Action categories` section
- [`src/reyn/config/embedding.py`](https://github.com/anthropics/reyn) — `ActionRetrievalConfig`
- [`docs/reference/config/reyn-yaml.md`](../../reference/config/reyn-yaml.md#action_retrieval-block) — config reference
