---
name: configure_rag_embedding_provider
description: Confirm a working embedding provider before running `rag_ingest` -- reyn routes every embedding call through litellm (direct API when `LITELLM_API_BASE` is unset, else a litellm proxy). Covers the pre-flight curl check and the API-key path (Case A); see `configure_rag_local_embedding_model` for the self-hosted local-model path (Case B). Read this before your first `rag_ingest` call, or when it reports an unreachable embedding endpoint.
---

# Configure the RAG embedding provider

Companion to `build_and_query_rag_corpus`. `rag_ingest` needs a working
embedding provider, or every chunk it embeds is wasted spend. Reyn depends
on litellm exclusively for embeddings -- no in-process model backend -- so
**every embedding call routes through `litellm`**: straight to the
provider's own API, or through a **litellm proxy** if `LITELLM_API_BASE`
is set (the same variable `call_llm` reads -- one proxy serves both).
Default classes: `light`/`standard` -> `openai/text-embedding-3-small`,
`strong` -> `openai/text-embedding-3-large`. A local model (no API key /
offline) is reached the same way -- behind a proxy, see
`configure_rag_local_embedding_model` (Case B).

## Pre-flight: confirm the endpoint actually answers (do this before `rag_ingest`)

One curl, before you spend anything on an ingest. **Transport-independent
by construction**: reyn always sends embedding requests to an
OpenAI-compatible `/embeddings` endpoint at whatever `LITELLM_API_BASE`
names -- proxy, direct API, or local server all look the same, so the same
one-liner verifies any of them:

```bash
curl -s "${LITELLM_API_BASE:-<your-endpoint>}/embeddings" \
  -H "Authorization: Bearer ${OPENAI_API_KEY:-dummy}" \
  -H "Content-Type: application/json" \
  -d '{"model": "<the model name your endpoint expects>", "input": "hello"}' \
  | jq '.data[0].embedding | length'
```

Replace `<your-endpoint>` / the model name / the key with your actual
values -- a shape to adapt, not a literal command. **Healthy**: prints a
positive integer (the embedding dimension, e.g. `1536`). Typical failures:
**401** wrong/missing API key; **404 / "model not found"** that model name
isn't registered at this endpoint (proxy `model_list` mismatch, or a wrong
direct-API model string); **400, unsupported param** (proxy only, see Case
B) -- proxy missing `litellm_settings.drop_params: true` (#1616);
**connection refused** -- nothing listening there, or `LITELLM_API_BASE`
points at the wrong address.

## Case A -- you have an embedding API key -- no proxy needed

This is the shortest path, and it does **not** go through a proxy at all:
`reyn secret set OPENAI_API_KEY` (or your provider's key), and stop.
With `LITELLM_API_BASE` unset, reyn's litellm client calls the provider's
API **directly** (`_proxy_kwargs()` returns nothing when the env var is
absent), so the default `standard` class (`openai/text-embedding-3-small`)
works with **no `reyn.yaml` edit, no proxy, and no `drop_params` setting**.
Run the pre-flight curl above against the provider's own endpoint (e.g.
`https://api.openai.com/v1`) to confirm.

If your organization already routes LLM traffic through a shared litellm
proxy, you're effectively in the Case B situation
(`configure_rag_local_embedding_model`) even though you have a key -- its
`drop_params` note applies to you too.

This skill covers the pre-flight check and the API-key path (Case A)
only -- see `configure_rag_local_embedding_model` for the self-hosted
local-model path (Case B) and how to pick a local embedding model.
