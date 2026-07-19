---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn embeddings]
---

# `reyn embeddings`

Inspect and manage the action embedding index that backs `search_actions`. Reads on-disk state only — no network probes; matches the cheap-by-default posture of `reyn mcp list`.

The action index rides the same pluggable `IndexBackend` doc-RAG sources use (one cosine implementation, one advisory-lock, one storage/dedup path — unified in FP-0057 Phase 0); its cache lives at `.reyn/cache/index/actions/` alongside other indexed sources. Pre-consolidation it had a separate, hand-rolled SQLite schema and cosine implementation under `.reyn/cache/action_index/` — that path is no longer read or written (clean-break; the cache is fully regenerable, so no migration was needed).

See [Concepts: enable semantic search](../../guide/for-users/enable-semantic-search.md) for the onboarding walkthrough, and [Concepts: RAG — Embedding configuration](../../concepts/data-retrieval/rag.md#embedding-configuration) for the underlying `embedding.classes` map.

## Synopsis

```
reyn embeddings status [--json]
reyn embeddings rebuild [<class_name>]
reyn embeddings clear
```

## Subcommands

### `status [--json]`

Show one row per configured embedding class with `name / backend / model / cache_path / size_mb / indexed_actions / last_built`. Reads `reyn.yaml` for the class list and `.reyn/cache/index/actions/index.db` for the current on-disk binding.

```bash
$ reyn embeddings status

NAME      BACKEND  MODEL                           CACHE_PATH                  SIZE_MB  ACTIONS  LAST_BUILT
────────────────────────────────────────────────────────────────────────────────────────────────────────────
light     litellm  openai/text-embedding-3-small   .reyn/cache/index/actions      0.31       87  (never)
standard  litellm  openai/text-embedding-3-small   .reyn/cache/index/actions      0.31       87  2026-05-27T19:02:00+00:00
strong    litellm  openai/text-embedding-3-large   .reyn/cache/index/actions      0.31        0  (never)
```

Every class's `backend` reads `litellm` (#3128 removed reyn's in-process sentence-transformers backend — reyn depends on litellm exclusively for embeddings now, whether the model runs at a provider's own API or behind an operator-run litellm proxy). `cache_path` / `size_mb` are the shared `.reyn/cache/index/actions/` SQLite — not a per-class download cache; there is nothing left for this command to manage other than that index. The SQLite cache stores one `model_class` at a time (= one row carries the current `indexed_actions` / `last_built`; the rest report `0 / "(never)"` for the build state, though `size_mb` is the same shared directory for every row). This avoids ambiguity about which class owns the on-disk index after a class swap.

`--json` emits the same data as a list of dicts for scripting:

```bash
reyn embeddings status --json | jq '.[] | select(.indexed_actions > 0)'
```

### `rebuild [<class_name>]`

Drop the on-disk action index SQLite + WAL sidecars + `.build.lock` marker so the next `reyn chat` session re-embeds. Does NOT itself trigger embedding; the next Session that uses `search_actions` re-runs `ActionEmbeddingIndex.build()`.

```bash
reyn embeddings rebuild
# removed action index cache: index.db, index.db-wal.
# The next chat session will re-embed.
```

A clean project state (= no `.reyn/cache/index/actions/index.db`) reports "nothing to rebuild" without erroring:

```bash
reyn embeddings rebuild
# no action index found at .reyn/cache/index/actions/index.db; nothing to rebuild.
```

`<class_name>` is accepted for forward compatibility with a future per-class cache. Today the SQLite layout stores one `model_class` at a time, so passing a name verifies the class exists in `reyn.yaml` and notes the cache-shared semantics:

```bash
reyn embeddings rebuild standard
# removed action index cache (shared across classes today): index.db, index.db-wal.
# The next chat session will re-embed for class 'standard'.
```

Unknown class names exit non-zero with the configured list inline:

```bash
reyn embeddings rebuild stnadard   # typo
# error: embedding class 'stnadard' not in reyn.yaml's embedding.classes.
# Configured: ['light', 'standard', 'strong']
```

### `clear`

Removes `.reyn/cache/index/actions/` (= SQLite index + WAL sidecars + build lock). #3128 removed reyn's in-process sentence-transformers backend, so `clear` no longer also wipes a downloaded-model cache — the SQLite action index is the only on-disk state this command manages; every embedding call routes through litellm (a provider's own API, or an operator-run litellm proxy), which owns any model cache on its own side, outside reyn's `.reyn/cache/` tree entirely.

```bash
reyn embeddings clear
# removed /path/to/.reyn/cache/index/actions
# freed ~0.31 MB
```

Useful for:

- **Cache corruption** — a partial write / interrupted build leaves the SQLite cache in a state that the next `embed()` call can't recover from on its own.
- **Reclaiming disk / starting fresh** — the index is fully regenerable (content-hash re-embeds only what changed), so wiping it is always safe; the next `search_actions` build re-creates it.

Absent paths are reported as skipped — calling `clear` on a clean install is a no-op.

## Related

- [Concepts: enable semantic search](../../guide/for-users/enable-semantic-search.md) — fresh-user setup
- [Concepts: universal catalog — `search_actions`](../../concepts/tools-integrations/universal-catalog.md#what-stays-out-of-phase-1) — the surface this index backs
- [Concepts: RAG — Embedding configuration](../../concepts/data-retrieval/rag.md#embedding-configuration) — `embedding.classes` map
- [`reyn secret`](secret.md) — set `OPENAI_API_KEY` for the LiteLLM-backed embedding classes
