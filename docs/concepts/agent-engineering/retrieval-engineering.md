---
type: concept
topic: architecture
audience: [human, agent]
---

# Retrieval Engineering

Feeding the right context into the agent at the right time — memory of past interactions, project-specific knowledge, external documentation, search results. Retrieval quality often dominates output quality more than model choice does. This is one of the constitution's two declared **honest thin areas** (see `CLAUDE.md`'s Constitution section) — the framing below leans toward stating what exists plainly rather than dressing up gaps.

## How reyn handles it

### `recall` — vector search over any indexed corpus

`recall` is a typed Control IR op the LLM calls directly: embed the query, run `index_query` per configured source, merge the top-K results globally. It runs over a pluggable `IndexBackend` (SQLite is the default, ≤100K chunks, sub-second query) — not a keyword/flat-index match.

Indexing a corpus is deliberately not a bundled one-command skill: a short safe-mode Python step reads your files, chunks them, and calls `embed_and_index()` once. **The differentiation from LangChain/LlamaIndex is where the retrieval call lives** — those give you a library you call from your own driver code; reyn's `recall` is a built-in tool the LLM itself calls during an ordinary `reyn chat` session, with no orchestration code required on the search side. There is no separate `recall_docs` mechanism — project documentation is retrieved the same way any other corpus is: index it once via `embed_and_index()`, then `recall` reaches it like any other source.

### Memory — a separate mechanism from RAG retrieval

Project- and agent-scoped memory (user preferences, project decisions, agent-specific habits) is a **distinct** mechanism from `recall`, not a special case of it: memory is read inline by the router on every chat turn (a `MEMORY.md` index merged from the shared + agent-scoped layers), not queried on demand via a tool call. See [Memory](../data-retrieval/memory.md) for the read/write path.

### Web retrieval

`web_search` and `web_fetch` are bundled Tier-1, default-allow tools — not something a workflow author has to wire up themselves.

## Where it's still thin

Being honest about scope rather than dressing it up:

- **Phase 1 only.** The framework foundation, the SQLite default backend, and the LiteLLM embedding passthrough are what currently ships. Vector store plugin variety (Qdrant / FAISS / Weaviate / Pinecone), advanced retrieval (rerank / HyDE / contextual retrieval), and RAG eval frameworks are explicitly post-1.0 territory — not a secret gap, a stated boundary. If you need that ecosystem today, LangChain / LlamaIndex are the better fit for it.
- **A framework, not a pipeline.** `recall` + a pluggable `IndexBackend` a safe-mode Python step calls directly is a foundation to build retrieval on, not a deterministic, fully-managed RAG pipeline. You own the chunking logic.
- **No bundled corpus-indexing skill.** Every corpus (docs included) needs its own short indexing script before `recall` can reach it — there is no `reyn index this repo` one-liner.

## See also

- `CLAUDE.md` (§ Constitution) — the Retrieval lens's pass-line and its explicit thin-area declaration
- [`docs/concepts/architecture/charter.md`](../architecture/charter.md) — the Retrieval row, grounded across all 7 feature families
- [`docs/concepts/data-retrieval/rag.md`](../data-retrieval/rag.md) — the full RAG framework, quick start, and Phase 1/2 scope boundary
- [`docs/concepts/data-retrieval/memory.md`](../data-retrieval/memory.md) — the separate memory mechanism
- [tool-contract-design.md](tool-contract-design.md) — how `recall` slots into the typed op contract
