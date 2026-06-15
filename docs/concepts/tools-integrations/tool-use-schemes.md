---
type: concept
topic: tool-use-schemes
audience: [human, agent]
---

# Tool-Use Schemes

How an agent's tools are shown to the LLM — and how the LLM's calls are turned
back into dispatched actions — is a **pluggable scheme**. Reyn ships four, and
you select one per layer in `reyn.yaml`. The defaults are `enumerate-all` for the
`chat` layer (#1657) and `universal-category` for `step` / `phase`; any layer can
be switched to another scheme via config.

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
name-hallucination on non-hot-list tools. This is why the chat default moved to
`enumerate-all` (#1657).

### `enumerate-all` (chat default, #1657)

Presents *every* usable tool flatly in the LLM's tool list and dispatches by
name — the plain, native-JSON baseline with no discovery indirection. **This is
the default for the `chat` layer** (#1657): flat-listing actions lets the LLM
invoke them directly, avoiding the `invoke_action` name-hallucination that the
discover-then-call indirection induced (measured ~30%→100% non-hot-list tool-use
on the chat path). Leaving `tool_use.chat` unset keeps it.

**Use when:** the default for chat — direct, deterministic name→dispatch. The
trade-off is a **visibility cost, not a weak-model penalty**: request size grows
linearly with the catalogue (H1 measured ~67 tools ≈ ~50KB of tool surface,
~3.2× the `universal-category` request) because every name is shown up front.
That visibility is precisely what fixes weak-model tool-use; the cost is tokens,
which only bites at very large catalogues (see `universal-category`).

### `universal-category`

The [universal action catalog](universal-catalog.md): every action — a skill, an
MCP tool, a memory entry, a file op, an indexed corpus — is addressed by a single
qualified name and reached through a small fixed set of wrappers (discover →
describe → invoke). The LLM-facing tool list stays constant as the catalogue
grows. The default for the `step` / `phase` layers; set `tool_use.chat:
universal-category` to use it for chat too.

**Use when:** a very large / fast-growing tool set where flat-listing every
action in the request would cost too many tokens — the wrappers keep the
LLM-facing tool list constant.

### `retrieval`

RAG-over-tools. Instead of presenting the whole catalogue, it presents a **search
tool**; the LLM searches, the OS re-presents only the matched actions as callable
tools, and the LLM calls one.

**Use when:** the tool set is **very large** and presenting it in full would cost
too many tokens — the search narrows the candidates before the call.

### `CodeAct`

Code-as-tools. The LLM writes a short Python snippet, and tool calls happen as
in-code `tool(...)` calls. The snippet runs in a **sandboxed subprocess**, and
each in-code call round-trips through the **same permission gate as a JSON tool
call** — a CodeAct call is gated at least as strictly as the equivalent JSON
call, plus sandbox containment.

**Use when:** running **weak / low-cost models**, where expressing tool use as
code measurably outperforms JSON tool-calling.

## Per-layer selection

The scheme is chosen independently for each of the three layers an agent runs:

```yaml
# reyn.yaml
tool_use:
  chat: enumerate-all         # top-level chat router (default, #1657)
  step: universal-category    # plan / skill steps (default)
  phase: universal-category   # OS phases (default)
```

Any layer can use any registered scheme; the others are unaffected. Full per-key
reference: [`reyn.yaml` § tool_use](../../reference/config/reyn-yaml.md#tool_use-block).

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

- [Universal Action Catalog](universal-catalog.md) — the internals of the `universal-category` scheme (step/phase default; chat alternative)
- [`reyn.yaml` § tool_use](../../reference/config/reyn-yaml.md#tool_use-block) — config reference
- [Permission model](../runtime/permission-model.md) — the gate every scheme dispatches through
