---
type: concept
topic: tool-use-schemes
audience: [human, agent]
---

# Tool-Use Schemes

How an agent's tools are shown to the LLM — and how the LLM's calls are turned
back into dispatched actions — is a **pluggable scheme**. Reyn ships four, and
you select one per layer in `reyn.yaml`. The default reproduces the standard
behaviour, so schemes are entirely opt-in.

The key invariant: **the scheme only changes the LLM-facing surface**. Every
tool call, whichever scheme produced it, is routed through the same OS gate —
exclusion check → permission check → dispatch. Swapping schemes never changes
what is allowed, only how the LLM is asked to express a call. See
[Permission model](../runtime/permission-model.md).

## The four schemes

### `universal-category` (default)

The [universal action catalog](universal-catalog.md): every action — a skill, an
MCP tool, a memory entry, a file op, an indexed corpus — is addressed by a single
qualified name and reached through a small fixed set of wrappers (discover →
describe → invoke). The LLM-facing tool list stays constant as the catalogue
grows. This is Reyn's shipped behaviour; leaving `tool_use` unset keeps it.

**Use when:** the default — a broad, growing tool set where you don't want the
tool list to scale with the number of resources.

### `enumerate-all`

Presents *every* usable tool flatly in the LLM's tool list and dispatches by
name — the plain, native-JSON baseline with no discovery indirection.

**Use when:** the tool set is **small** and you want maximum determinism and the
simplest possible mapping from the LLM's call to a dispatch. Note this is *not* a
weak-model aid — a flat JSON tool list is the hardest surface for weak models.

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
  chat: universal-category    # top-level chat router
  step: universal-category    # plan / skill steps
  phase: universal-category   # OS phases
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

- [Universal Action Catalog](universal-catalog.md) — the internals of the default `universal-category` scheme
- [`reyn.yaml` § tool_use](../../reference/config/reyn-yaml.md#tool_use-block) — config reference
- [Permission model](../runtime/permission-model.md) — the gate every scheme dispatches through
