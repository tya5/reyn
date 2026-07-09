---
type: concept
topic: architecture
audience: [human, agent]
---

# Retrieval Engineering

> **Status: stale.** This page was written against the deleted phase-graph
> skill engine (`recall_memory` as a stdlib workflow invoked via a
> "preprocessor") and predates two features that have since shipped —
> confirmed via `docs/feature-map.md`'s Memory & RAG section:
> **(1)** `recall` is now a typed Control IR op doing vector similarity
> search (`index_query` per source, merge top-K) over a pluggable
> `IndexBackend`, not the keyword/index matching described below as current
> and "a likely next step" for vector retrieval; **(2)** `web_search` /
> `web_fetch` are now bundled Tier-1 default-allow tools, contradicting the
> "no web search or external retrieval primitive" claim below. A rewrite is
> tracked as a follow-up; in the meantime see
> [`docs/concepts/data-retrieval/rag.md`](../data-retrieval/rag.md) and
> [`docs/concepts/architecture/charter.md`](../architecture/charter.md)
> (Retrieval row — one of the constitution's two honest thin areas) for the
> current, grounded story.

Feeding the right context into the agent at the right time — memory of past interactions, project-specific knowledge, external documentation, search results. Retrieval quality often dominates output quality more than model choice does.

## How reyn handles it

Two retrieval mechanisms today, both expressed as ordinary stdlib workflows:

### `recall_memory`

Pulls facts from project- and user-scope memory stores:

| Scope | Lives at | Holds |
|-------|----------|-------|
| Global | `~/.reyn/memory/` | Facts about the user (role, preferences) |
| Project | `.reyn/memory/` | Facts about the current project |

Both scopes share the same shape (a `MEMORY.md` index plus one `<slug>.md` per entry) and are read together; project entries surface first. Workflows consume retrieval results via the preprocessor:

```yaml
preprocessor:
  - run_skill:
      skill: recall_memory
      input:
        type: user_message
        data: { text: "what does the user prefer?" }
      into: relevant_memories
```

The phase reads `input.relevant_memories` like any other field — it does not need to know the data came from a preprocessor.

### `reyn chat` automatic recall

In chat mode, every turn implicitly calls `recall_memory` (`top-k` configurable via `chat.memory.recall_top_k`) and offers `write_memory` a chance to persist anything new every few turns. The retrieval cadence is configured, not hand-orchestrated.

## Where it's still thin

This is the lens where reyn currently has the most ground to cover.

**`recall_docs` is not yet implemented.** The plan is for it to be the symmetric counterpart of `recall_memory` — a stdlib workflow that retrieves from the project's docs the way `recall_memory` retrieves from memory. Until it ships, workflows that need doc context transcribe the relevant passages directly into phase instructions. This works but it's manual, and the transcription drifts from the source.

**Memory matching is keyword/index-based, not vector.** `MEMORY.md` is a flat index; `recall_memory` returns entries that match the query by keyword and metadata. At a few dozen entries this is fine. At a few hundred it will start missing relevant matches. Vector retrieval (or hybrid keyword + vector) is a likely next step, but the API surface — a stdlib workflow with a well-typed input — is already the right shape for swapping the implementation later.

**No web search or external retrieval primitive.** Workflows that need to fetch from the web invoke MCP search tools when configured; reyn does not bundle a default web retrieval workflow. The intent is to keep the OS workflow-agnostic (P7) — retrieval kinds are added by writing workflows, not by changing the runtime.

## What this lens is really asking

Retrieval engineering isn't just "did we find the doc?" — it's "did the agent see the doc *at the moment a decision depended on it*?" reyn's preprocessor mechanism is the answer to the timing half: retrieval runs deterministically, before the LLM call, with results placed where the phase already expects them. The remaining work is on the matching half (better recall, broader sources).

## See also

- [../data-retrieval/memory.md](../data-retrieval/memory.md) — concept (memory is read inline by the router on every chat turn)
- [tool-contract-design.md](tool-contract-design.md) — how retrieval slots into the contract

