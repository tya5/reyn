## Configure a local RAG embedding model (Case B)

Companion to `configure-embedding-provider.md` (which covers the pre-flight
check and Case A, the API-key path). This file is Case B: no embedding API
key, so a local embedding model runs behind a litellm proxy -- the proxy is
what turns that local model into the OpenAI-compatible endpoint reyn
expects; reyn itself never talks to the local server directly.

**Step 1 -- start a local embedding server.** Ollama is the lightest setup;
reference commands below, **verify on your own machine, versions/ports may
differ**:

```bash
ollama pull nomic-embed-text
ollama serve   # if not already running as a background service
curl http://localhost:11434/api/embeddings -d '{"model": "nomic-embed-text", "prompt": "hello"}'
```

(Alternatives: HuggingFace `text-embeddings-inference`, or `infinity` --
both also OpenAI-compatible.)

**Step 2 -- register it in the litellm proxy's `config.yaml`** (syntax
confirmed against https://docs.litellm.ai/docs/proxy/embedding and
https://docs.litellm.ai/docs/proxy/configs, checked 2026-07):

```yaml
model_list:
  - model_name: text-embedding-3-small   # see the naming rule below
    litellm_params:
      model: ollama/nomic-embed-text
      api_base: http://localhost:11434

litellm_settings:
  drop_params: true   # required -- see #1616
```

Restart the proxy after editing.

**Step 3 -- point reyn at the proxy:**

```bash
export LITELLM_API_BASE=http://localhost:4000   # your proxy's address
```

**Naming rule (read before choosing a model name):** when
`LITELLM_API_BASE` is set, reyn strips the resolved model string's leading
`provider/` segment before sending it to the proxy -- `openai/foo` arrives
at the proxy as plain `foo`. So the proxy's `model_list[].model_name` must
equal **everything after the first `/`** of whichever reyn-side model
string you use:

- **(a) Simplest, no `reyn.yaml` edit.** Keep the default `standard`/`light`
  class (`openai/text-embedding-3-small`); register the proxy's
  `model_name` as `text-embedding-3-small` (Step 2 above) -- the local
  model now answers under reyn's default class name.
- **(b) Or add an explicit class** in `reyn.yaml` (`embedding.classes.local.
  model: openai/nomic-embed-text`); the proxy's `model_name` must then be
  `nomic-embed-text` (everything after `openai/`), and you'd pass
  `embedding_model: "local"` to `rag_ingest.ingest` / `rag_query.query`.

**Step 4 -- confirm it end to end.** Re-run `configure-embedding-provider.md`'s
pre-flight curl first (cheapest check). Then run a real ingest + query (see
`run-ingest-and-query-workflow.md`): a non-empty `[{id, distance, metadata}, ...]`
list is the real signal -- `chunks_upserted > 0` on the ingest response
alone does not prove the vectors are meaningful. An empty query result with
a populated db usually means a naming mismatch (Step 3 above); `rag_ingest`
returning "blocked" means a server, not the embedding endpoint, is
unreachable -- see the router SKILL.md's "Prerequisites".

### Choosing a local model -- pick once, it's expensive to change

**"One sqlite file = one embedding model" makes this choice sticky** --
swapping later means a full re-ingest into a *new* `output_db`, not an
in-place update (see `corpus-internals-schema-tuning-and-backend-swap.md`
for the `dim`-based mechanism enforcing this). Decide with these axes before
your first big ingest:

- **Language.** English-only corpus -> a small English-only model is
  enough. Japanese, Chinese, or mixed-language -> use a multilingual
  model; an English-only model's cross-lingual recall is poor.
- **Size vs. recall.** A smaller model embeds faster and costs less compute
  per query; a larger model trades that for better recall. Reference point
  (measured, not vendor-claimed): `all-MiniLM-L6-v2` (22 MB, 384-dim,
  English-only, fastest) vs. `multilingual-e5-small` (118 MB, 50 languages,
  better cross-lingual recall) vs. OpenAI's `text-embedding-3-small` API
  (~5 pp higher MTEB, at API cost) -- same tradeoffs whether served locally
  (this file) or as an API (`configure-embedding-provider.md`'s Case A).
  See `docs/guide/for-users/enable-semantic-search.md` § "Choosing a local
  model (Case B)" (same comparison, for `search_actions`).
- **Server ecosystem.** Via Ollama, `nomic-embed-text` is the easiest
  openai-compatible option. Via HuggingFace `text-embeddings-inference` or
  `infinity`, `bge-*` / `e5-*` are common. **Verify size/dimension/language
  on the model's own card** -- unlike the two figures above, not
  independently confirmed here.

In short: **fast English corpus -> a small English model
(`nomic-embed-text` is a reasonable Ollama default). Japanese/multilingual
-> a multilingual model. Best recall + already have an API key -> skip
this file, use `configure-embedding-provider.md`'s Case A instead.**
