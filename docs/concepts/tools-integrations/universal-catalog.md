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
MCP tool, read a memory, search an indexed corpus, … — is addressed by its
**one** name and dispatched through
`invoke_action`. Discovery happens through `list_actions` and detail
introspection through `describe_action`; semantic / natural-language
discovery uses `search_actions` (embedding-backed).

**Status update: tool presentation is now a pluggable scheme, not
a single fixed path.** Since Phase 6 (2026-05-16) the wrapper-only path was
briefly the sole production behaviour, but an owner-driven H1 fix later flipped
the `chat` layer's own default to `enumerate-all` — a flat, no-wrapper tool
list — because flat listing stops `invoke_action` name-hallucination (30%→100%
direct tool-use accuracy). `universal-category` (this page's wrapper path)
remains a registered scheme, reachable when an operator sets `tool_use.scheme:
universal-category` in `reyn.yaml`. See [Tool-Use Schemes](tool-use-schemes.md)
for the full, current model — the sections below describe the
`universal-category` scheme's own mechanics, not which layer uses it by default.
(#2768 removed the dead phase-graph-era `step`/`phase` tool-use layers.)

When this wrapper path is active for a layer, the handlers (`invoke_skill` /
`call_mcp_tool` / …) remain in the registry as
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
| `exec` | Sandboxed argv execution | run argv under the sandbox backend |
| `skill_management` | Skill definitions | list / install (local dir or git source) |
| `pipeline` | Registered pipelines | list, or run one to completion |
| `pipeline_management` | Pipeline definitions | install (local dir or git source) |
| `presentation_management` | Named presentation templates | install (inline declarative blueprint) |
| `plugin_management` | Installed plugins | install (builtin/local/git) / uninstall / list |
| `knowledge` | The operator's own skill/memory/repo content | `search_knowledge` (embedding-gated) |
| `embedding` | — (compute op, no stored resource) | `embed` a batch of texts into vectors |
| `hooks` | This session's own push hooks | `hooks_add` a hook / `emit_hook_event` an LLM-authored event |

> **FP-0066 P1b**: the `rag_operation` category (`list_sources` / multi-source
> `semantic_search` / drop source) is **retired outright** — those were the
> agent-facing layer-1 in-core RAG tools, a pre-audience-split relic. See
> [proposal 0066 §9](../../deep-dives/proposals/0066-retrieval-two-groups-two-axes.md)
> and [RAG concepts](../data-retrieval/rag.md).

The `mcp` category provides six verb_object actions that cover the LLM-visible surface:

| Action | Purpose |
|---|---|
| `mcp_search_registry`  | Search the official MCP registry for new servers |
| `mcp_install_registry` | Install a server from the official MCP registry |
| `mcp_install_package`  | Install via a third-party package (npm/pypi/docker) or a GitHub repo URL |
| `mcp_install_local`    | Register a local command (e.g. LLM-authored script) as an MCP server |
| `list_mcp_servers`     | Enumerate installed servers |
| `list_mcp_tools`     | Enumerate one server's tools as `<server>__<tool>` ids |
| `mcp_call_tool`      | Call a tool by `<server>__<tool>` id with `tool_args` |
| `mcp_drop_server`    | Remove an installed server |

`exec` is gated by `is_exec_available()` — it only appears when a real
sandbox backend (= not `"noop"`) is configured. The rest are always
visible.

**Every category enumerates a fixed set of verbs.** A resource — a stored
memory, an indexed corpus, an installed MCP tool, a registered pipeline — is an
**argument** to a verb, never an enumerated action of its own, so the number of
actions the LLM is shown does not grow with what the operator has accumulated.
Where collapsing a resource category removed the only surface that *named* those
resources, a constant-count discovery verb replaces it (`list_memory`,
`list_mcp_tools`, `pipeline_list`,
`skill_list`).

## Action names (#3429)

An action's name is its **flat registry tool name** — `read_file`,
`web_search`, `mcp_call_tool` — and there is exactly one of them. A category is
a browsing axis (`list_actions(category=["file"])`), never part of the name.

**§D18 used to specify a second, qualified spelling** — `<category>__<verb>`,
so `read_file` was also `file__read` — with a parser and a routing table
mapping one to the other. Two names for one operation meant every subsystem
keyed on a tool name had to decide whether to handle both; a census of the 11
that exist found 4 with explicit two-form compensation (the permission axis'
`_expand_tool_forms`, the op-gate's alias map, …) and 7 without (result
normalisation, canonicalization declarations, permission-denied hints, the
advertisement gate, the exclusive-wrapper strip list, the `routing_decided`
audit-event, action-usage tracking). Fixing the 7 would have left the twelfth
subsystem to flip the same coin, so the second name is what was removed.

The naming convention for the surviving name is
[`docs/reference/runtime/tool-naming.md`](../../reference/runtime/tool-naming.md).
`tests/tools/test_no_qualified_tool_names_3429.py` is the gate: it walks the live
registry, the membership table, the categories tuple, and the assembled
`tools=` payload, and fails on any `__` in a name. (Deletion is a state; the
gate is the property.)

A useful side effect: every name now satisfies OpenAI's native function-name
grammar `^[a-zA-Z0-9_-]{1,64}$` by construction. The dotted categories that
once made qualified names LiteLLM-proxy-dependent are long gone, and
`tests/tools/test_qualified_name_provider_grammar_1456.py` pins the property.

## The 3 wrappers

### `list_actions(category, filter, offset, limit) → {items, total}`

Browses the catalogue alphabetically. `category` is a list of category
names (omit or pass `[]` to include everything visible). `filter` is a
case-insensitive substring match against `action_name` and
`short_description`. `offset` / `limit` paginate. Items carry
`action_name` and a short description; long descriptions are
deliberately omitted so the listing stays compact.

In the **weak-model landing design**, a narrowed-category result instead
carries each item's full `description` and `input_schema` (the triple
`action_name` + `description` + `input_schema`), so the common flow is
`list_actions` → `invoke_action` with no intervening `describe_action`. See
[Weak-model discovery + selection reliability](#weak-model-discovery-selection-reliability).

### `describe_action(action_name) → {action_name, description, input_schema, metadata}`

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
[Membership](#membership-what-an-action-name-means)). The wrapper is transparent: the
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
| read a stored memory | `list_memory` | `read_memory_body({layer: "shared", slug: "..."})` |
| call an MCP tool | `list_mcp_tools` | `mcp_call_tool({tool: "<server>__<tool>", tool_args})` |
| run a registered pipeline | `pipeline_list` | `run_pipeline({name: "greet", input: {...}})` |
| search your own knowledge | — | `search_knowledge({query: "..."})` |

`read_memory_body` takes an explicit `layer` (`shared` or `agent`), so
both memory layers are reachable through the catalog.

The `<server>__<tool>` string in the MCP row is the MCP **server's** own tool
identifier, an argument value in a namespace Reyn does not own — not a Reyn
tool name.

**#3429 removed the author-time exception.** Two resource forms used to
*resolve* without being enumerated, on the reasoning that a name a human already
typed costs zero payload: `pipeline__<name>` (which the pipeline guide taught)
and `mcp__<server>__<tool>` (a `tool:` step in a pipeline DSL file). That
reasoning holds for payload and is silent about naming — each was a SECOND name
for a verb that already existed, and a second name is what every name-keyed
subsystem has to remember to handle. A pipeline step now names the flat tool and
passes the resource id as an ordinary argument, exactly as the table above
shows.

## Membership (what an action name means)

An **action** is a registered `ToolDefinition`, addressed by its flat registry
name. A **category** is a browsing axis over that set. The membership table
lives in
[`src/reyn/tools/universal_dispatch.py`](https://github.com/anthropics/reyn) and
is **pure** — no I/O, no state, no live invocation:

- **`_CATEGORY_ACTIONS`** — a **closed table** of category → the flat tool names
  that category browses. It is the only table an enumerator may read, which is
  what keeps the payload constant.

`invoke_action` then:

1. Checks `action_name` against `KNOWN_ACTION_NAMES` (`require_known_action`).
2. Looks the name up in the unified `ToolRegistry`.
3. Calls that tool's own handler with the args the caller sent, unchanged.

**There is no rewriting step between (1) and (3).** Until #3429 there was: the
name arrived in a `<category>__<verb>` spelling that this layer mapped to a flat
registry name, and two of the mappings also reshaped args (`cluster`→`path`,
`message`→`request`) in ways no advertised schema declared — capability that
existed only on the qualified route. The args the model sends are the args the
handler receives, which is what "transparent wrapper" was always supposed to
mean.

If the name is not an action, dispatch raises `UnknownActionError` carrying
`difflib`-ranked suggestions from the live, availability-aware action set.

## Error response (§D12)

When `invoke_action` or `describe_action` receives an unknown
`action_name`, the response is structured rather than raised:

```json
{
  "error": "Unknown action 'read_fil'",
  "reason": "not a known action name",
  "suggestions": ["read_file", "edit_file", "delete_file"],
  "hint": "Use list_actions(category=[...]) to discover the correct name."
}
```

`suggestions` come from `difflib.get_close_matches` against the live,
availability-aware action set.
The hint always points back at `list_actions` so the LLM has an
obvious recovery move.

## Visibility gating (§D14)

Some categories are visibility-gated by the runtime environment:

| Predicate | Effect |
|---|---|
| `is_search_available(embedding_enabled)` | Whether `search_actions` appears in tools= (Phase 2) |
| `is_exec_available(sandbox_backend)` | Whether `exec` appears in `list_actions` enumeration |

The gates are pure functions; the runtime supplies the configuration
values from `embedding.enabled` (FP-0066 §7) and the resolved
sandbox backend. Hidden categories appear neither in the
`list_actions` `category=` enum nor in any enumeration result.

## System prompt placement (§D9)

When `tool_use.universal_wrappers_enabled` is true, the router
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

1. **Satisficing** — the model invokes a familiar action (`write_file`)
   instead of discovering a better-fit one (`edit_file`), because the
   familiar action is "good enough".
2. **Discovery-skip** — the model does not proactively call `list_actions`;
   it guesses an action name from training priors, often malformed
   (`file.write`, `file__read`).

*Status: the no-names system prompt and the `edit_file` cross-reference are
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
  training-**known** concepts (`read_file` / `write_file`) the weak model
  recalls the concept and emits a malformed approximation rather than
  discovering the exact name. Known-action *selection* is handled by the
  mechanical mandate below, not by name-hiding.

### `list_actions` returns name + description + schema

When `list_actions(category=[…])` narrows to a bounded set, each item carries
the **full triple** — `action_name`, `description`, and `input_schema`:

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
("for a partial edit, prefer `edit_file`") is ignored (0/20 followed it); an
unconditional mandate ("edits MUST use `edit_file`, NOT `write_file`") is
followed (edit 3 / write 1).

The router therefore gates a set of mechanical system-prompt mandates on the
model tier (`router_model: light` → on; `strong` → off):

- **`list_actions`-first** — the first tool call MUST be `list_actions`
  before reading, writing, or editing anything.
- **`edit_file`-MUST** — partial / surgical edits must use `edit_file`,
  not `write_file`.

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
update at the top of this page: the `tool_use.scheme` (x `tool_use.transport`)
selector generalizes this flag's *selection* role, and the chat layer's own
scheme default (`enumerate-all`) does not route through this flag at all. The flag itself remains live for the
`universal-category` scheme (catalog-wrapper vs direct-tool presentation).

`ToolUseConfig.universal_wrappers_enabled` (#4552 PR-3: moved from the
retired `ActionRetrievalConfig`) defaults to `True` in production. Direct
callers of `build_tools` or `build_system_prompt` that don't pass this flag
(e.g. unit-test fixtures constructing a `FakeRouterHost`) keep the legacy
off behavior because `RouterLoop` reads the flag through a `getattr(host,
"get_universal_wrappers_enabled", None)` fallback that returns
`False` when the method is missing. The dual path keeps LLMReplay
fixtures byte-valid while production routers get the new tools.

To opt out, add the following to `reyn.yaml`:

```yaml
tool_use:
  universal_wrappers_enabled: false
```

## `embedding.enabled` default + opt-in

**FP-0043 Phase 4** defaulted `ActionRetrievalConfig.embedding_class` to
`"local-mini"` (= a since-removed in-process `sentence-transformers`
backend, see below), making `search_actions` automatically available
for any fresh installation that had the (then-required) local-embed
extras installed — no `reyn.yaml` edits required. The
**semantic-search-opt-in fix** reverted this: a truthy default made
reyn attempt a Hugging Face model download at chat startup even on
zero-config / offline installs, surfacing as a startup warning when
the download failed — contradicting the project's standing principle
that semantic search is opt-in.

**FP-0066 §7 (2026-07) config clean-break**: the fragmented
`ActionRetrievalConfig.embedding_class` field (which conflated the
on/off decision with which embedding class to use) is retired,
clean-break, no alias. It splits into `embedding.enabled: bool`
(default `False` — off) and `embedding.default_class` (already
existed, default `"standard"` — which model to use when enabled).
With `embedding.enabled: false`, no embedding index build is attempted
at all — `search_actions` is simply absent from `tools=` per the §D14
gate below, silently (there is nothing to fail or warn about).
Operators opt in explicitly via `embedding.enabled: true` in
`reyn.yaml` (optionally paired with a non-default
`embedding.default_class: standard` — or `light` / `strong`, all
OpenAI-backed, no local install — or a custom `embedding.classes`
entry pointing at any litellm-routable model, including a local model
served behind an operator-run litellm proxy) — see
[Guide: enable semantic search](../../guide/for-users/enable-semantic-search.md).

**#3128 removed reyn's in-process `sentence-transformers` backend**
(`local-mini` / `local-e5`, the `reyn[local-embed]` extras, and the
`Session.__init__` missing-extras probe that used to degrade an
ST-backed class with absent extras to `None`). Reyn depends on litellm
exclusively for embeddings now — every configured class, built-in or
custom, resolves the same way, so there is no separate "extras missing"
degrade path to document; a class that names an unreachable endpoint
simply fails the embed call the same way any other litellm call would,
surfaced through the normal error path rather than a silent `None`
degrade. Setting `embedding.enabled` to `false` opts out of
`search_actions` entirely.

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
  pointing operators at [Guide: enable semantic
  search](../../guide/for-users/enable-semantic-search.md) /
  `reyn embeddings status` so the config path is self-discoverable
  mid-chat. Off by default (opt-in only, per the semantic-search-opt-in
  fix); once opted in, the OpenAI-backed classes (`light` / `standard`
  / `strong`) and any operator-defined litellm-routable class are
  equally usable. See
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

**Retired (#4552, 2026-08):** a hot-list mechanism (`action_retrieval.hot_list_n`,
a top-N freq+recency direct-alias projection, default off) previously existed
here — removed, owner directive: the mechanism's role is gone, superseded by
`list_actions` as the canonical discovery path.

## Reference files

- [`src/reyn/tools/universal_catalog.py`](https://github.com/anthropics/reyn) — `CATEGORIES`, 4 ToolDefinitions, D14 helpers, real handlers
- [`src/reyn/tools/universal_dispatch.py`](https://github.com/anthropics/reyn) — `_CATEGORY_ACTIONS` membership table, `require_known_action`, `UnknownActionError`, `suggest_similar_names`
- [`src/reyn/runtime/router_tools.py`](https://github.com/anthropics/reyn) — `build_tools` integration (flag-gated wrappers)
- [`src/reyn/runtime/router_system_prompt.py`](https://github.com/anthropics/reyn) — `## Action categories` section
- [`src/reyn/config/execution.py`](https://github.com/anthropics/reyn) — `ToolUseConfig.universal_wrappers_enabled` (#4552 PR-3: moved from the retired `ActionRetrievalConfig`)
- [`docs/reference/config/reyn-yaml.md`](../../reference/config/reyn-yaml.md#tool_use-block) — config reference
