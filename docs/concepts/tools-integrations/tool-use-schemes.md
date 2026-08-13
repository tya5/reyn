---
type: concept
topic: tool-use-schemes
audience: [human, agent]
---

# Tool-Use Schemes

How an agent's tools are shown to the LLM — and how the LLM's calls are turned
back into dispatched actions — is a **pluggable scheme**. Reyn ships four, and
you select one for the chat layer in `reyn.yaml`. The default is `enumerate-all`;
the chat layer can be switched to another scheme via config.

The key invariant: **the scheme only changes the LLM-facing surface**. Every
tool call, whichever scheme produced it, is routed through the same OS gate —
exclusion check → permission check → dispatch. Swapping schemes never changes
what is allowed, only how the LLM is asked to express a call. See
[Permission model](../runtime/permission-model.md).

## The four schemes

A cross-scheme finding (H1): **tool-name visibility predicts invocation
success**. Schemes that place the callable name directly in the LLM-facing
surface (`enumerate-all`, `CodeAct`) let the model invoke without guessing;
schemes that put the name behind an indirection it must first traverse
(`universal-category`'s discover→invoke, `retrieval`'s search-first) invite
name-hallucination on direct tools. This is why the chat default moved to
`enumerate-all`.

### `enumerate-all` (chat default)

Presents *every* usable tool flatly in the LLM's tool list and dispatches by
name — the plain, native-JSON baseline with no discovery indirection. **This is
the default for the `chat` layer**: flat-listing actions lets the LLM
invoke them directly, avoiding the `invoke_action` name-hallucination that the
discover-then-call indirection induced (measured ~30%→100% direct tool-use
on the chat path). Leaving `tool_use.scheme` unset keeps it.

**Use when:** the default for chat — direct, deterministic name→dispatch. The
trade-off is a **visibility cost, not a weak-model penalty**: request size grows
linearly with the catalogue (H1 measured ~67 tools ≈ ~50KB of tool surface,
~3.2× the `universal-category` request) because every name is shown up front.
That visibility is precisely what fixes weak-model tool-use; the cost is tokens,
which only bites at very large catalogues (see `universal-category`).

### `universal-category`

The [universal action catalog](universal-catalog.md): every action — a workflow, an
MCP tool, a memory entry, a file op, an indexed corpus — is addressed by a single
qualified name and reached through a small fixed set of wrappers (discover →
describe → invoke). The LLM-facing tool list stays constant as the catalogue
grows. Opt in for chat by setting `tool_use.scheme: category` (the presentation-axis name; it resolves to the registered `universal-category` scheme).

**Use when:** a very large / fast-growing tool set where flat-listing every
action in the request would cost too many tokens — the wrappers keep the
LLM-facing tool list constant.

### `retrieval`

RAG-over-tools. Instead of presenting the whole catalogue, it presents a **search
tool**; the LLM searches, the OS re-presents only the matched actions as callable
tools, and the LLM calls one. (That round trip is how the `tool_calls` cell
narrows; the `content_fence` cell below reaches the same paradigm without one.)
A **supported opt-in** alternative to the chat
default — it requires `embedding.enabled: true` (FP-0066 §7) with a configured
embedding provider (the search is semantic). Because matching is semantic, its
quality depends on the embedding index, so it suits stable, well-indexed
catalogues.

**Use when:** the tool set is **very large** and presenting it in full would cost
too many tokens — the search narrows the candidates before the call.

**Measured (weak-model 4-way refresh):** retrieval
is clean on single-step reads and read→transform→write chains, but on **read-heavy
multi-file** tasks the weak model reads files sequentially and the search→re-present
per-round overhead makes it slow (timeout-prone) — *correct-but-slow*, a tuning cost,
not a cognition gap (uncapped, it completes the same task). So retrieval is a
**catalogue-scaling opt-in, not a weak-default replacement**: `enumerate-all` remains
the weak-model chat default (highest task-completion and fastest-terminating in the
comparison). See the 4-way refresh journal under
`docs/deep-dives/journal/dogfood/2026-06-17-4way-retrieval-refresh/`.

### `CodeAct`

Code-as-tools. The LLM writes a short Python snippet, and tool calls happen as
in-code `tool(...)` calls. The snippet runs in a **sandboxed subprocess**, and
each in-code call round-trips through the **same permission gate as a JSON tool
call** — a CodeAct call is gated at least as strictly as the equivalent JSON
call, plus sandbox containment.

**Use when:** running **weak / low-cost models**, where expressing tool use as
code measurably outperforms JSON tool-calling.

CodeAct is the `enumerate-all` presentation expressed over the `content_fence`
**transport** (FP-0066 P4, #3247) — the same full flat catalog as
`enumerate-all`, but the model expresses calls as fenced code instead of
native tool calls. Select it via `tool_use.scheme: enumerate-all` +
`tool_use.transport: content_fence` (see below), not a `codeact` scheme name.

### `universal-category` over `content_fence`

The two axes compose: the `category` presentation can also be expressed over
the `content_fence` transport. The model writes fenced Python exactly as CodeAct
does, but the functions it is shown are the catalog **wrappers**
(`list_actions` / `describe_action` / `invoke_action`) plus the base tools —
so a call reads

```python
result = invoke_action(action_name="read_file", args={"path": "README.md"})
```

and the code-API **does not grow with the catalog**, which is the whole point of
the `category` presentation and is preserved unchanged by the transport swap.

**Use when:** a weak / low-cost model does better writing code than emitting
JSON tool calls (the `content_fence` reason) **and** the catalog is large enough
that listing every action up front is the wrong trade (the `category` reason).
CodeAct gives up the second: it shows every action. Select it with
`tool_use.scheme: category` + `tool_use.transport: content_fence`.

### `retrieval` over `content_fence`

The third composition: the search-first presentation as a code-API. The model is
shown `search_actions` / `describe_action` / `invoke_action` plus the base tools
and **no `list_actions`** — discovery here is a search, not a listing, which is
what separates it from the `category` cell above:

```python
hits = search_actions(query="read a file")
result = invoke_action(action_name="read_file", args={"path": "README.md"})
```

**It costs one round trip less than its `tool_calls` sibling**, and this is the
reason the pair is interesting rather than merely symmetrical. Over `tool_calls`,
narrowing requires the OS to re-present the matched actions as a new `tools=`
payload, because a payload can only change *between* LLM calls — that is what
`RePresent` is for. Over `content_fence` the search result is an ordinary value
inside the snippet, so the search and the call happen in the same turn. It also
*cannot* use `RePresent`: this transport's whole tool-use surface is the system
prompt, which is built once per turn, so a re-presented code-API would have
nowhere to land.

**Use when:** the catalog is large enough that browsing it by category is the
wrong entry point *and* a weak / low-cost model does better writing code than
emitting JSON tool calls. Requires `embedding.enabled: true`, like every
`retrieval` cell. When the embedding index is not ready, the cell falls back to
listing the flat catalog rather than showing a search that would return nothing —
the same degrade its `tool_calls` sibling performs, and for the same reason (a
search backed by no index strands the model on empty results).

Select it with `tool_use.scheme: retrieval` + `tool_use.transport:
content_fence`.

## Chat-layer selection

Tool-use decomposes into two config keys: `tool_use.scheme` (the
**presentation** — `category` / `enumerate-all` / `retrieval`) and
`tool_use.transport` (how the model expresses a chosen action — `tool_calls`
/ `content_fence`). Every combination of those values is implemented; a pair
naming a `scheme` or `transport` reyn does not have fails loud at config-parse
time rather than falling back to a default.

```yaml
# reyn.yaml
tool_use:
  scheme: enumerate-all       # top-level chat router (default)
  transport: tool_calls       # default
```

To select CodeAct:

```yaml
# reyn.yaml
tool_use:
  scheme: enumerate-all
  transport: content_fence
```

To select the small-surface code-API (`category` over `content_fence`):

```yaml
# reyn.yaml
tool_use:
  scheme: category
  transport: content_fence
```

To select the search-first code-API (`retrieval` over `content_fence`):

```yaml
# reyn.yaml
tool_use:
  scheme: retrieval
  transport: content_fence
  # requires embedding.enabled: true
```

Full per-key reference: [`reyn.yaml` § tool_use](../../reference/config/reyn-yaml.md#tool_use-block).

## Why this is safe to swap

The scheme is *presentation and parsing* — pluggable data the OS reads. The
load-bearing parts are not part of the scheme:

- The LLM still may only call tools the OS has made eligible (the candidate set);
  a scheme cannot widen that.
- Every call still passes the exclusion + permission gate before dispatch.
- Validation of the call and its result is unchanged.

So choosing `enumerate-all`, `retrieval`, or `CodeAct` changes how the model is
asked to use tools, not what it is permitted to do. The presentation varies; the
gate is constant.

## See also

- [Universal Action Catalog](universal-catalog.md) — the internals of the `universal-category` scheme (a chat-layer opt-in)
- [`reyn.yaml` § tool_use](../../reference/config/reyn-yaml.md#tool_use-block) — config reference
- [Permission model](../runtime/permission-model.md) — the gate every scheme dispatches through
