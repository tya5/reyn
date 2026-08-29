---
type: concept
topic: architecture
audience: [human, agent]
---

# Retrieval Engineering

Feeding the right context into the agent at the right time — memory of past interactions, project-specific knowledge, external documentation, search results. Retrieval quality often dominates output quality more than model choice does. This is one of the constitution's two declared **honest thin areas** (see `CLAUDE.md`'s Constitution section) — the framing below leans toward stating what exists plainly rather than dressing up gaps.

## How reyn handles it

### `semantic_search` — OS-internal only, not something the LLM calls

`semantic_search` (FP-0057 Phase 2a; renamed from `recall`) is a Control-IR op over a pluggable `IndexBackend` (SQLite is the default, ≤100K chunks, sub-second query) — per-source-model embed the query, run `index_query` per configured source, merge the top-K results (never comparing scores across different embedding spaces). **As of FP-0066 P1b/P1c, this is OS-internal substrate, not an agent- or user-facing surface**: there is no LLM tool, no safe-mode Python entry point, and no CLI command left that can create or query a source in this store — see [Concepts: RAG](../data-retrieval/rag.md) for the full retirement history and what still builds on the substrate internally (`search_actions` today; `search_knowledge` too, since FP-0066 P3c). The `semantic_search`/`index_update`/`index_drop` op kinds themselves are kept by intent, not left over — they're the substrate later FP-0066 §8 ingest phases build on, which is also why they stay in the permission vocabulary despite having no agent-facing tool today (#5495).

**If you want an agent to search your own documents**, use the builtin user RAG (proposal 0063) instead — two bundled pipelines that ingest a folder of documents into an external vector store you name and query it, agent-callable end-to-end, no Python step to write. See [Build a RAG corpus](../../guide/for-users/build-a-rag-corpus.md). That is a different store with a different setup from the in-core index this section describes; the two share only the `embed` primitive.

### Memory — a separate mechanism from RAG retrieval

Project- and agent-scoped memory (user preferences, project decisions, agent-specific habits) is a **distinct** mechanism from `semantic_search`, not a special case of it: memory is read inline by the router on every chat turn (a `MEMORY.md` index merged from the shared + agent-scoped layers), not queried on demand via a tool call. See [Memory](../data-retrieval/memory.md) for the read/write path.

### Web retrieval

`web_search` and `web_fetch` are bundled Tier-1, default-allow tools — not something a workflow author has to wire up themselves.

## Where it's still thin

Being honest about scope rather than dressing it up:

- **Documents only, via the user RAG plugin.** The agent-callable, agent-facing retrieval story is the FP-0063 plugin's bundled ingest/query pipelines — a folder of documents in, an external vector store you name, queryable end-to-end. Advanced retrieval (rerank / HyDE / contextual retrieval) and a built-in RAG-eval framework are not shipped; the pipeline's own YAML is the intended extension point (copy it, swap the chunker/vector-store server), not a separate plugin API.
- **No general-purpose "index anything and have the LLM search it" surface.** The OS-internal store (`semantic_search`/`index_update`) that used to serve this role is retired as an agent/user-facing mechanism (FP-0066 P1b/P1c) — see the section above. Non-document corpora (execution traces, custom logs) have no supported indexing path today.
- **No bundled corpus-indexing skill for the user-RAG plugin either.** Feeding it a folder of documents is what's bundled; adapting an arbitrary source into that shape is on you.

## See also

- `CLAUDE.md` (§ Constitution) — the Retrieval lens's pass-line and its explicit thin-area declaration
- [`docs/concepts/architecture/charter.md`](../architecture/charter.md) — the Retrieval row, grounded across all 7 feature families
- [`docs/concepts/data-retrieval/rag.md`](../data-retrieval/rag.md) — the full retirement history of the in-core index and the user-RAG plugin's scope
- [`docs/guide/for-users/build-a-rag-corpus.md`](../../guide/for-users/build-a-rag-corpus.md) — setting up the user-RAG plugin
- [`docs/concepts/data-retrieval/memory.md`](../data-retrieval/memory.md) — the separate memory mechanism
