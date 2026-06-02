---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn embeddings]
---

# `reyn embeddings`

Inspect and manage the action embedding index that backs `search_actions`. Reads on-disk state only — no network probes; matches the cheap-by-default posture of `reyn mcp list`.

See [Concepts: enable semantic search](../../guide/for-users/enable-semantic-search.md) for the onboarding walkthrough, and [Concepts: RAG — Embedding configuration](../../concepts/data-retrieval/rag.md#embedding-configuration) for the underlying `embedding.classes` map.

## Synopsis

```
reyn embeddings status [--json]
reyn embeddings rebuild [<class_name>]
reyn embeddings clear
```

## Subcommands

### `status [--json]`

Show one row per configured embedding class with `name / backend / model / cache_path / size_mb / indexed_actions / last_built`. Reads `reyn.yaml` for the class list and `.reyn/action_index/index.db` for the current on-disk binding.

```bash
$ reyn embeddings status

NAME        BACKEND                MODEL                                                 CACHE_PATH                                            SIZE_MB  ACTIONS  LAST_BUILT
─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
light       litellm                openai/text-embedding-3-small                         .reyn/action_index                                       0.00        0  (never)
local-e5    sentence-transformers  sentence-transformers/intfloat/multilingual-e5-small  ~/.cache/reyn/sentence-transformers                      0.00        0  (never)
local-mini  sentence-transformers  sentence-transformers/all-MiniLM-L6-v2                ~/.cache/reyn/sentence-transformers                     22.43      87  2026-05-27T19:02:00+00:00
standard    litellm                openai/text-embedding-3-small                         .reyn/action_index                                       0.00        0  (never)
strong      litellm                openai/text-embedding-3-large                         .reyn/action_index                                       0.00        0  (never)
```

The SQLite cache stores one `model_class` at a time (= one row carries the current `indexed_actions` / `last_built`; the rest report `0 / "(never)"`). This avoids ambiguity about which class owns the on-disk index after a class swap.

`--json` emits the same data as a list of dicts for scripting:

```bash
reyn embeddings status --json | jq '.[] | select(.indexed_actions > 0)'
```

### `rebuild [<class_name>]`

Drop the on-disk action index SQLite + WAL sidecars + `.build.lock` marker so the next `reyn chat` session re-embeds. Does NOT itself trigger embedding; the next ChatSession that uses `search_actions` re-runs `ActionEmbeddingIndex.build()`.

```bash
reyn embeddings rebuild
# removed action index cache: index.db, index.db-wal.
# The next chat session will re-embed.
```

A clean project state (= no `.reyn/action_index/index.db`) reports "nothing to rebuild" without erroring:

```bash
reyn embeddings rebuild
# no action index found at .reyn/action_index/index.db; nothing to rebuild.
```

`<class_name>` is accepted for forward compatibility with a future per-class cache. Today the SQLite layout stores one `model_class` at a time, so passing a name verifies the class exists in `reyn.yaml` and notes the cache-shared semantics:

```bash
reyn embeddings rebuild local-mini
# removed action index cache (shared across classes today): index.db, index.db-wal.
# The next chat session will re-embed for class 'local-mini'.
```

Unknown class names exit non-zero with the configured list inline:

```bash
reyn embeddings rebuild lcal-mini   # typo
# error: embedding class 'lcal-mini' not in reyn.yaml's embedding.classes.
# Configured: ['light', 'local-e5', 'local-mini', 'standard', 'strong']
```

### `clear`

Aggressive cleanup. Removes BOTH `.reyn/action_index/` (= SQLite + build lock) AND the sentence-transformers HF model cache directory (resolved via `REYN_CACHE_DIR > XDG_CACHE_HOME > ~/.cache/reyn` precedence, matching the runtime backend).

```bash
reyn embeddings clear
# removed /path/to/.reyn/action_index
# removed /Users/.../.cache/reyn/sentence-transformers
# freed ~22.43 MB
```

Useful for:

- **Cache corruption** — a partial download / interrupted load leaves the cache in a state that the next `embed()` call can't recover from on its own.
- **Backend swap reclamation** — operator switched `action_retrieval.embedding_class` from `local-mini` to `standard` (= OpenAI) and wants to reclaim the 22 MB local model. `rebuild` keeps the model cache; `clear` removes it.

Absent paths are reported as skipped — calling `clear` on a clean install is a no-op.

## Related

- [Concepts: enable semantic search](../../guide/for-users/enable-semantic-search.md) — fresh-user setup
- [Concepts: universal catalog — `search_actions`](../../concepts/tools-integrations/universal-catalog.md#what-stays-out-of-phase-1) — the surface this index backs
- [Concepts: RAG — Embedding configuration](../../concepts/data-retrieval/rag.md#embedding-configuration) — `embedding.classes` map
- [`reyn secret`](secret.md) — set `OPENAI_API_KEY` for the LiteLLM-backed embedding classes
