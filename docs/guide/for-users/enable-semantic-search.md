# Enable semantic action search

`reyn chat` ships with two ways for the LLM to discover what it can do: a fast **`list_actions`** browser (= category-prefix enumeration, always available) and a **`search_actions`** semantic search (= natural-language queries against an embedding index of every action). This guide walks through enabling the semantic path.

> **TL;DR**: `search_actions` is **off by default** (semantic search is opt-in project-wide). If you already have an embedding API key, `reyn secret set OPENAI_API_KEY` then set `action_retrieval.embedding_class: standard` in `reyn.yaml` — no proxy, no extra install. If you'd rather run a local model with no API key, put it behind a **litellm proxy** and point reyn at it (see [Case B](#case-b-no-embedding-api-contract-litellm-proxy-a-local-model) below).

## When you'd want it

`search_actions` is the difference between:

- **Without it**: the LLM has to guess which category your intent belongs to (`file` / `mcp` / `memory_operation` / …) and run `list_actions(category=[...])` to enumerate. For natural-language asks like _"find an action that converts PDF to text"_ the LLM may also try and refuse if it doesn't immediately spot a match.
- **With it**: the LLM runs `search_actions(query="PDF to text")` and gets a top-K relevance-ranked list across every category. It can then `describe_action` or `invoke_action` directly.

`action_retrieval.embedding_class` defaults to `null` (off) — semantic search is opt-in, so an explicit `reyn.yaml` setting is required. With no class configured, `search_actions` is gated **out** of the LLM's tool list (see [visibility gate](../../concepts/tools-integrations/universal-catalog.md#what-stays-out-of-phase-1)) — silently, with no startup warning, since nothing is attempted.

## Reyn depends on litellm exclusively for embeddings

Reyn has **no in-process embedding backend**. Every embedding call — action retrieval, `semantic_search`, the builtin RAG plugin — routes through `litellm`: straight to the provider's own API, or through a **litellm proxy** if the env var `LITELLM_API_BASE` is set (the same variable `call_llm` reads — one proxy serves both chat and embeddings). Built-in classes: `light` / `standard` → `openai/text-embedding-3-small`, `strong` → `openai/text-embedding-3-large`.

There are exactly two setups, and which one you want depends on whether you already have an embedding API contract.

### Pre-flight: confirm the endpoint actually answers (do this before opting in)

One curl, before you spend anything on an embedding call. This check is **transport-independent by construction**: reyn always sends embedding requests to an OpenAI-compatible `/embeddings` endpoint at whatever `LITELLM_API_BASE` names — a litellm proxy, a direct embedding API, or a local server all look the same from reyn's side, so the same one-liner verifies any of them:

```bash
curl -s "${LITELLM_API_BASE:-<your-endpoint>}/embeddings" \
  -H "Authorization: Bearer ${OPENAI_API_KEY:-dummy}" \
  -H "Content-Type: application/json" \
  -d '{"model": "<the model name your endpoint expects>", "input": "hello"}' \
  | jq '.data[0].embedding | length'
```

Replace `<your-endpoint>` / the model name / the key with your actual values — this is a shape to adapt, not a literal command. **Healthy**: prints a positive integer (the embedding dimension, e.g. `1536`) — `data[0].embedding` came back as a non-empty float array. Typical failure signatures:

- **401** — wrong or missing API key.
- **404 / "model not found"** — that model name isn't registered at this endpoint (proxy `model_list` mismatch, or a wrong direct-API model string).
- **400, unsupported param** — *only relevant when routing through a proxy* (see Case B): the proxy is missing `litellm_settings.drop_params: true` (#1616).
- **connection refused** — nothing is listening at that endpoint, or `LITELLM_API_BASE` points at the wrong address.

## Case A — you have an embedding API key — no proxy needed

This is the shortest path, and it does **not** go through a proxy at all:

```bash
reyn secret set OPENAI_API_KEY
# enter your sk-... key when prompted
```

Then opt in explicitly in `reyn.yaml` (the default is `null` / off):

```yaml
action_retrieval:
  embedding_class: standard   # = openai/text-embedding-3-small
```

With `LITELLM_API_BASE` unset, reyn's litellm client calls the provider's API **directly**, so `standard` works with **no proxy and no `drop_params` setting** — the client already passes `drop_params=True` on every call, which is only a no-op when a proxy sits in between (see Case B). Run the pre-flight curl above against the provider's own endpoint (e.g. `https://api.openai.com/v1`) to confirm, then start a chat session — `search_actions` builds its index eagerly on the next cold start.

If your organization already routes LLM traffic through a shared litellm proxy, you're effectively in the Case B situation below (proxy in the path) even though you have a key — the proxy's `drop_params` note applies to you too.

## Case B — no embedding API contract → litellm proxy + a local model

No key, and you don't want one — or you want an offline/air-gapped setup: run a local embedding model behind a litellm proxy. The proxy is what turns that local model into the OpenAI-compatible endpoint reyn already expects; reyn itself never talks to the local server directly. This is also how `search_actions` works fully offline once the local model is cached — no Hugging Face reach from reyn at any point, since reyn only ever talks to the proxy.

**Step 1 — start a local embedding server.** Ollama is the lightest setup (openai-compatible embeddings out of the box); reference commands below, **verify on your own machine, versions/ports may differ**:

```bash
ollama pull nomic-embed-text
ollama serve   # if not already running as a background service
curl http://localhost:11434/api/embeddings -d '{"model": "nomic-embed-text", "prompt": "hello"}'
```

(Alternatives, one line each: HuggingFace `text-embeddings-inference`, or `infinity` — both also expose an OpenAI-compatible embeddings endpoint.)

**Step 2 — register it in the litellm proxy's `config.yaml`.** Syntax confirmed against litellm's own docs (https://docs.litellm.ai/docs/proxy/embedding, https://docs.litellm.ai/docs/proxy/configs):

```yaml
model_list:
  - model_name: text-embedding-3-small   # see the naming rule below
    litellm_params:
      model: ollama/nomic-embed-text
      api_base: http://localhost:11434

litellm_settings:
  drop_params: true   # required -- see the 400 failure signature above (#1616)
```

Restart the proxy after editing.

**Step 3 — point reyn at the proxy:**

```bash
export LITELLM_API_BASE=http://localhost:4000   # your proxy's address
```

**Naming rule (read before choosing a model name):** when `LITELLM_API_BASE` is set, reyn strips the resolved model string's leading `provider/` segment before sending it to the proxy — `openai/foo` arrives at the proxy as plain `foo`. So the proxy's `model_list[].model_name` must equal **everything after the first `/`** of whichever reyn-side model string you use:

- **(a) Simplest — no `reyn.yaml` edit at all.** Keep the built-in `standard` class (`openai/text-embedding-3-small`) and register the proxy's `model_name` as `text-embedding-3-small` (as in Step 2 above) — the local model now answers under reyn's default class name:
  ```yaml
  action_retrieval:
    embedding_class: standard
  ```
- **(b) Or add an explicit class**, e.g. in `reyn.yaml`:
  ```yaml
  embedding:
    classes:
      local:
        model: openai/nomic-embed-text
  action_retrieval:
    embedding_class: local
  ```
  Here the proxy's `model_name` must be `nomic-embed-text` (everything after `openai/`).

**Step 4 — confirm it end to end.** Re-run the pre-flight curl above first (cheapest check), then start a chat session and confirm `search_actions` appears in the tool list (or check `reyn embeddings status`, below). A non-empty index (`ACTIONS > 0`) is the real signal.

### Choosing a local model (Case B) — pick once, it's expensive to change

Swapping the embedding model later means every embedding it produced (the action index, and any RAG source using the same class) is invalidated and needs re-embedding — decide with these axes before you commit:

- **Language.** English-only usage → a small English-only model is enough. Japanese, Chinese, or mixed-language prompts → use a multilingual model; an English-only model's cross-lingual recall is poor.
- **Size vs. recall.** A smaller model embeds faster and costs less compute per query; a larger model trades that for better recall. As a reference point (measured, not vendor-claimed): `all-MiniLM-L6-v2` (22 MB, 384-dim, English-only, fastest) vs. `multilingual-e5-small` (118 MB, 50 languages, better cross-lingual recall) vs. OpenAI's `text-embedding-3-small` API (~5 pp higher MTEB than `multilingual-e5-small`, at API cost). These numbers describe the *models themselves* — served locally through Ollama/TEI/infinity behind your proxy, or as OpenAI's own API in Case A — not a reyn-specific backend.
- **Server ecosystem.** Serving via Ollama (Step 1 above), the easiest openai-compatible option is `nomic-embed-text`. Serving via HuggingFace `text-embeddings-inference` or `infinity` instead, the `bge-*` / `e5-*` families are common choices there. Verify the exact size/dimension/language numbers on the model's own card.

In short: **English usage, want it fast → a small English model (`nomic-embed-text` is a reasonable Ollama default). Japanese/multilingual → a multilingual model. Want the best recall and already have an API key → skip Case B, use Case A instead.**

## How Reyn tells you when it's not configured

If you skip both Case A and Case B and still ask the LLM to "find an action for …", the response from `list_actions` carries a structured **hint** field pointing at this guide. The LLM relays the hint to you so the install is self-discoverable mid-chat — no need to memorise this guide. The hint disappears the moment `search_actions` becomes available.

## Troubleshooting

**`search_actions` doesn't appear in the LLM's tool list** — either `action_retrieval.embedding_class` is still `null`, or the index hasn't finished building yet (= cold start, a handful of seconds). Check `reyn embeddings status` — a configured class with `ACTIONS = 0` and `LAST_BUILT = (never)` means the build hasn't completed:

```bash
$ reyn embeddings status

NAME      BACKEND  MODEL                           CACHE_PATH                  SIZE_MB  ACTIONS  LAST_BUILT
────────────────────────────────────────────────────────────────────────────────────────────────────────────
light     litellm  openai/text-embedding-3-small   .reyn/cache/index/actions      0.31       87  (never)
standard  litellm  openai/text-embedding-3-small   .reyn/cache/index/actions      0.31       87  2026-05-27T19:02:00+00:00
strong    litellm  openai/text-embedding-3-large   .reyn/cache/index/actions      0.31        0  (never)
```

**The pre-flight curl fails** — see the failure signatures under [§ Pre-flight](#pre-flight-confirm-the-endpoint-actually-answers-do-this-before-opting-in) above: 401 (bad key), 404 (model name / proxy `model_list` mismatch), 400 unsupported-param (missing proxy `drop_params: true`, #1616), connection refused (nothing listening / wrong `LITELLM_API_BASE`).

**Swapping classes returns stale results** — Reyn's action index stores one embedding class at a time. Class swaps trigger automatic re-embedding on the next session, but you can force it eagerly with `reyn embeddings rebuild`.

**Old `mcp.server` / `agent.peer` category mentioned by the LLM** — the LLM's training data may pre-date a Reyn collapse refactor. `list_actions(category=["mcp.server"])` post-Reyn-0.4 returns an [explicit error with a legacy → current mapping](../../concepts/tools-integrations/universal-catalog.md#category-validation--legacy-redirect) so the LLM self-corrects in a single retry.

## Related

- [`reyn embeddings` CLI reference](../../reference/cli/embeddings.md) — status / rebuild / clear
- [Concepts: universal catalog](../../concepts/tools-integrations/universal-catalog.md) — how `list_actions` / `search_actions` fit together
- [Concepts: RAG](../../concepts/data-retrieval/rag.md#embedding-configuration) — the underlying `embedding.classes` config map (shared with document recall)
- [Configure the RAG embedding provider skill](https://github.com/tya5/reyn/blob/main/src/reyn/builtin/plugins/rag/skills/configure_rag_embedding_provider/SKILL.md) (+ [local-model companion](https://github.com/tya5/reyn/blob/main/src/reyn/builtin/plugins/rag/skills/configure_rag_local_embedding_model/SKILL.md)) — the same litellm-proxy embedding setup (Case A/B), written for the builtin RAG plugin; this guide mirrors it for `search_actions`
