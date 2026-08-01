---
type: concept
topic: operational-intelligence
audience: [human, agent]
---

# Operational Intelligence

**This capability does not exist today.** The workflow this page used to describe — indexing Reyn's own P6 audit-event log (`.reyn/events/*.jsonl`) into the in-core RAG store via a safe-mode `index_update()` step, then querying execution history semantically via `semantic_search` — has no surviving entry point. FP-0066 P1b retired the four agent-facing layer-1 RAG tools (`semantic_search`, `index_update`, `drop_source`, `list_rag_sources`); FP-0066 P1c then retired the remaining safe-mode `index_update()` Python entry point and the `reyn source` CLI command group. There is no operator- or agent-facing way to add to, remove from, or search the in-core store any more — see [Concepts: RAG](rag.md) for the full retirement history.

This is a deliberate removal, not an oversight — recorded here so a future reader can tell "forgotten" from "decided" rather than inferring either from the absence.

**Current entry points**, for context: `search_actions` (tool/mcp/pipeline catalog search) is live today; a `search_knowledge` verb (skill/memory/repo retrieval) is planned. Neither is a general-purpose "index an arbitrary corpus" surface. For agent-facing document retrieval, the current path is the FP-0063 user-RAG plugin (an external vector store, documents only) — see [Build a RAG corpus](../../guide/for-users/build-a-rag-corpus.md). No workflow for indexing something like an event log through that plugin has been built or exercised; if one becomes worth doing, it belongs in its own design/issue rather than a claim on this page.

## See also

- [Concepts: RAG](rag.md) — the full retirement history and what remains
- [Concepts: Events](../runtime/events.md) — P6 event log structure and current event taxonomy
- [FP-0009: Operational Intelligence](../../deep-dives/proposals/0009-operational-intelligence.md) — original design rationale (historical; the mechanism it proposed has since been retired)
