# FP-0002: index_docs / recall_docs — unified document retrieval skill

**Status**: done (= ADR-0033 Accepted, Phase 1 landed 2026-05-10, commit `1e6f153`)
**Proposed**: 2026-05-09
**Landed**: 2026-05-10 (= 12 commits, d2db332..1e6f153)
**Author**: Research session (eager-shaw-389d9d)
**ADR**: [0033](../decisions/0033-rag-extensible-os.md) (= confirmed design + landed implementation)

---

## Summary

Current memory retrieval is limited to keyword matching plus full inline expansion of all entries into the system prompt.
By implementing `index_docs` (chunk splitting + embedding) and `recall_docs` (catalog filter + semantic top-K) as stdlib skills,
we achieve unified semantic search across memory, src, and arbitrary files.
The `recall_memory` concept is absorbed into `recall_docs(sources=[{type: "memory"}])`.

---

## Motivation

### Current constraints

```
memory retrieval → keyword substring matching (find_one)
                 → all entries inlined into system prompt
                 → no semantic search / no size cap
```

- Paraphrases and synonyms are completely missed
- System prompt grows as sessions get longer
- No search mechanism exists for docs / src (`recall_docs` not yet implemented)
- The indexing pipeline — the hardest part of RAG — cannot be customized without code changes

### Core design insight

> The differentiator is being able to describe and override the hardest part of RAG — the indexing pipeline — in natural language (skill.md).

LangChain / LlamaIndex require the index pipeline to be written in Python code.
In Reyn, it is described through `index_docs` skill Phase instructions + preprocessors,
and project-specific document structures can be handled with skill overrides alone.

---

## Proposed implementation

### Overall structure

```
index_docs  (stdlib skill)
  Phase 1 — strategy   : LLM inspects samples and decides chunking strategy
  Phase 2 — apply      : Python preprocessor applies strategy to all files
                         → embed op for vectorization → save to .reyn/index/

recall_docs (stdlib skill)
  Phase 1 — retrieve   : Python preprocessor does catalog filter → semantic top-K
  Fallback             : when no index, use catalog + size cap and pass directly
```

### Source types

`sources` is required (no implicit default).

| type | Path | Chunk unit |
|---|---|---|
| `memory` | `.reyn/memory/*.md` | 1 entry = 1 chunk |
| `src` | `src/**/*.py` etc. | function / class boundaries (described in skill) |
| `files` | arbitrary path | Markdown structural split (described in skill) |

**Specialization per document type is done by skill authors via override skills**:

```
stdlib/index_docs        ← generic framework (stdlib)
project/index_src        ← Python code specialization (skill author)
project/index_design     ← custom format specialization (skill author)
```

### Context limit mitigation

We never stream all sources into a single completion.

- **`iterate` op**: fetch file list, one file = one completion for chunk decisions
- **Determinism principle**: LLM decides strategy once; application is handled by the Python preprocessor

```
Phase 1 (LLM, 1 completion)
  input: file list + sample files
  output: ChunkStrategy artifact (boundary_rules, overlap_ratio, etc.)

Phase 2 (preprocessor + iterate)
  input: ChunkStrategy
  process: apply to all files → embed op → save to .reyn/index/
```

### Index storage and P5 / P6

| Storage location | Contents |
|---|---|
| `.reyn/index/<source_hash>/` | chunk vectors + ChunkMetadata (file storage) |
| WAL | only `embed` op completion + `content_hash` + `embedding_model` recorded |

Vector data (tens of MB scale) is not stored in the WAL (JSONL).
Crash recovery uses `content_hash` + `embedding_model` to skip unchanged chunks.

Two conditions that invalidate the index:
- `content_hash` changes → content modified → re-embed
- `embedding_model` changes → vector space incompatible → re-embed

### OS addition: `ChunkMetadata` model

```python
# added to schemas/models.py
class ChunkMetadata(BaseModel):
    source_path: str          # file path or memory slug
    source_type: str          # label assigned by skill (OS does not interpret the value)
    content_hash: str         # change detection / re-indexing decision
    embedding_model: str      # vector space compatibility management
    chunk_index: int          # position within source
    size_tokens: int          # context budget management
    parent_context: str | None = None  # heading / class / function name (for citation)
    extra: dict = {}          # domain-specific fields the skill may freely add
```

The OS does not interpret or branch on `source_type` values (P7 compliant).
Catalog filtering is handled by code on the `recall_docs` skill side.

### OS addition: `embed` op

```python
# schemas/models.py
class EmbedIROp(BaseModel):
    kind: Literal["embed"]
    texts: list[str]
    model: str = "text-embedding-3-small"

# op_runtime/registry.py
OP_PURITY["embed"] = OpPurity.external  # subject to WAL caching

# op_runtime/embed.py
async def handle(op: EmbedIROp, ctx: OpContext, ...) -> dict:
    vectors = await embedding_client.embed(op.texts, model=op.model)
    return {"kind": "embed", "vectors": vectors}
```

### recall_docs fallback

```
index present → catalog filter (ChunkMetadata) → semantic top-K
no index      → catalog enumerate → filter by token limit → pass directly to LLM
```

---

## Open design decisions

| Item | Status |
|---|---|
| Handling of `router_system_prompt.py` during `recall_memory` → `recall_docs` migration | TBD |

---

## Dependencies

- `src/reyn/schemas/models.py` — add `ChunkMetadata` + `EmbedIROp`
- `src/reyn/op_runtime/embed.py` — embed op handler (new)
- `src/reyn/op_runtime/registry.py` — add `embed` to `OP_KIND_MODEL_MAP` / `OP_PURITY`
- `src/reyn/memory/memory.py` — `find_one()` becomes legacy after `recall_docs` migration
- `embedding` library (e.g., `openai`) — new dependency if not already present

Prerequisite PRs: none (can be implemented independently)

---

## Cost estimate

**Total: LARGE**

| Task | Cost | Notes |
|---|---|---|
| `embed` op implementation | SMALL | 3 touch points + embedding client |
| `ChunkMetadata` model | SMALL | add Pydantic model only |
| `.reyn/index/` storage design | MEDIUM | file structure + diff detection logic |
| `index_docs` stdlib skill | MEDIUM | Phase 1 (strategy) + Phase 2 (iterate + apply) |
| `recall_docs` stdlib skill | MEDIUM | catalog filter + semantic search + fallback |
| `recall_memory` replacement | MEDIUM | deep router coupling requires separate discussion |

Bottlenecks are **storage design** and **router migration**. The embed op itself is SMALL.

---

## Related

- `src/reyn/memory/memory.py` — current keyword-only implementation
- `src/reyn/web/routers/a2a.py` — reference for the current memory injection flow
- `docs/deep-dives/research/landscape/reyn-strategic-priorities.md` — recall_docs gap noted
- CoALA (arXiv:2309.02427) — Episodic / Semantic / Procedural classification
- Anthropic Contextual Retrieval (2024) — technique for adding context to chunks
