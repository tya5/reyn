# Enable semantic action search

`reyn chat` ships with two ways for the LLM to discover what it can do: a fast **`list_actions`** browser (= category-prefix enumeration, always available) and a **`search_actions`** semantic search (= natural-language queries against an embedding index of every action). This guide walks through enabling the semantic path.

> **TL;DR**: run `pip install 'reyn[local-embed]'` once and `search_actions` becomes usable with no credentials. If you'd rather use OpenAI's embedding API (slightly higher quality), set `action_retrieval.embedding_class: standard` in `reyn.yaml` after `reyn secret set OPENAI_API_KEY`.

## When you'd want it

`search_actions` is the difference between:

- **Without it**: the LLM has to guess which category your intent belongs to (`file` / `mcp` / `memory.entry` / …) and run `list_actions(category=[...])` to enumerate. For natural-language asks like _"find an action that converts PDF to text"_ the LLM may also try and refuse if it doesn't immediately spot a match.
- **With it**: the LLM runs `search_actions(query="PDF to text")` and gets a top-K relevance-ranked list across every category. It can then `describe_action` or `invoke_action` directly.

For a fresh `reyn` install with no embedding class configured, `search_actions` is gated **out** of the LLM's tool list (= the [§D14 visibility gate](../../concepts/universal-catalog.md#what-stays-out-of-phase-1)). The LLM never sees it; chat-driven discovery falls back to `list_actions` only.

## Path A — local sentence-transformers (recommended for first-time users)

```bash
pip install 'reyn[local-embed]'
```

That's it. The `local-embed` extras install `sentence-transformers` + `torch`, and Reyn's default `action_retrieval.embedding_class` switches to `local-mini` (= `all-MiniLM-L6-v2`, 22 MB, 384-dim, English).

The first time `reyn chat` reaches `search_actions`, the model downloads (~5–10 s on a typical connection) and the embedding index builds. The TUI Memory tab shows a `⟳ loading…` row during the download and a `✓ loaded · all-MiniLM-L6-v2 · 384d` row when done; subsequent sessions warm-start from the local cache in <1 s.

### What you get

- **Zero credentials** — no API key required. Everything runs locally.
- **Zero per-query cost** — the `local-mini` model embeds queries in ~30–80 ms on a typical laptop CPU.
- **Offline-capable** — once the model is cached, semantic search works without network access.
- **`reyn embeddings status`** to inspect the cache state at any time:

```bash
$ reyn embeddings status
NAME        BACKEND                MODEL                                  ACTIONS  LAST_BUILT
local-mini  sentence-transformers  sentence-transformers/all-MiniLM-L6-v2     87  2026-05-27T19:02:00+00:00
```

### Multilingual content

If your prompts include Japanese / Chinese / European languages, swap to `local-e5` (= `multilingual-e5-small`, 118 MB, 50 languages, better cross-lingual recall):

```yaml
# reyn.yaml
action_retrieval:
  embedding_class: local-e5
```

After the swap, `reyn embeddings rebuild` drops the old cache so the next session re-embeds with the new model.

## Path B — OpenAI embeddings (slightly higher quality)

If you'd rather pay per query for marginally better recall (= the OpenAI text-embedding-3-small model scores ~5 pp higher on MTEB than `multilingual-e5-small`):

```bash
reyn secret set OPENAI_API_KEY
# enter your sk-... key when prompted
```

Then in `reyn.yaml`:

```yaml
action_retrieval:
  embedding_class: standard   # = openai/text-embedding-3-small
```

No `pip` install needed; the LiteLLM client is already a base dependency. The HTTP round-trip adds ~150–300 ms per query versus the local path; embedding cost is ~$0.00002 per chat session (= negligible).

## GPU acceleration (optional)

If you have a CUDA / Apple-Silicon GPU and want sentence-transformers to use it:

```bash
export REYN_EMBED_DEVICE=mps    # macOS Apple Silicon
export REYN_EMBED_DEVICE=cuda   # NVIDIA GPU
```

Default is `cpu`. The encode latency drops to ~5–15 ms per query on `mps`, which is enough of a step-change to be perceptible in long chat sessions. Invalid values warn and fall back to `cpu` rather than failing.

## How Reyn tells you when it's not configured

If you skip both Path A and Path B and still ask the LLM to "find an action for …", the response from `list_actions` carries a structured **hint** field listing the install / config paths above. The LLM relays the hint to you so the install is self-discoverable mid-chat — no need to memorise this guide. The hint disappears the moment `search_actions` becomes available.

## Troubleshooting

**`search_actions` doesn't appear in the LLM's tool list** — the embedding index hasn't finished building yet (= cold start, ~5–10 s) OR the configured class points at a backend whose extras aren't installed. Check `reyn embeddings status` — a configured class with `ACTIONS = 0` and `LAST_BUILT = (never)` means the build hasn't completed.

**"failed to load \<model>" in TUI / events** — partial cache state. Run `reyn embeddings clear` to wipe and start fresh; the next chat session re-downloads cleanly.

**Swapping classes returns stale results** — Reyn's cache stores one `model_class` at a time. Class swaps trigger automatic re-embedding on the next session, but you can force it eagerly with `reyn embeddings rebuild`.

**Old `mcp.server` / `agent.peer` category mentioned by the LLM** — the LLM's training data may pre-date a Reyn collapse refactor. `list_actions(category=["mcp.server"])` post-Reyn-0.4 returns an [explicit error with a legacy → current mapping](../../concepts/universal-catalog.md#category-validation--legacy-redirect) so the LLM self-corrects in a single retry.

## Related

- [`reyn embeddings` CLI reference](../../reference/cli/embeddings.md) — status / rebuild / clear
- [Concepts: universal catalog](../../concepts/universal-catalog.md) — how `list_actions` / `search_actions` fit together
- [Concepts: RAG](../../concepts/rag.md#embedding-configuration) — the underlying `embedding.classes` config map (shared with document recall)
