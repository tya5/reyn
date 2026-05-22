---
type: journal
topic: dogfood
audience: [agent, human]
status: open
---

# Known future challenges — SP intent-routing ceiling on weak tier

Cases where the V18 chat-router SP (= 4-intent multi-step routing, landed
2026-05-23) doesn't reliably guide `gemini-2.5-flash-lite` to the right
path, but `gemini-2.5-flash` (strong tier) does. The dogfood batch is
weak-tier only by policy, so these are filed here as future-challenge
rather than carry-over.

## Ceiling shape: catalog name space overlap

When the user's question keyword overlaps with a catalog category core
name (`mcp.*`, `skill__*`, `web__*`), `flash-lite` follows the catalog
attractor (= `list_actions(category=['<that-category>'])`) and exhausts
the discovery loop without falling back to
`invoke_action(reyn.source__read, README.md)`.

The structural cause was verified with a 24-chain experiment matrix
(see chain-replay methodology in `scripts/llm_replay.py --chain`):

| Query class | weak-tier hit | strong-tier hit |
|---|---|---|
| Non-overlap (workspace / permission / events) | partial (2-3/3) | 3/3 |
| Overlap MCP / skill | 0/3 | 3/3 |
| Overlap A2A (compound external-HTTP semantics) | 0/3 | 1/3 |

## Specific scenarios on the future-challenge list

### W5-S5 (`a2a_task_lifecycle_status_poll`) — partial give-up even at strong

- **Query** (JA): 「reyn web を起動して A2A エンドポイントにタスクを投げ、 GET /a2a/tasks/<id> でステータスを確認するにはどうすればいい?」
- **Path expected**: `invoke_action(reyn.source__read, README.md)` →
  README A2A section → synthesize covering `message/send` JSON-RPC +
  `GET /a2a/tasks/<id>` + `reyn web` boot order.
- **V18 weak result**: 0/3 (catalog overlap with `web__*` and `mcp.*`)
- **V18 strong result**: 1/3 (compound external-HTTP semantics still
  pulls 2/3 toward `web__search` / `list_actions(filter='web')`)
- **Hypothesised resolutions** (= for a future PR, not now):
  - RAG indexing of `docs/concepts/a2a.ja.md` + a `recall` call surfaces
    the A2A section by semantic similarity even when the keyword doesn't
    match the catalog substring filter.
  - A scenario-level strong-tier opt-in for compound-semantics queries.

### W5-S5 family — Q2 MCP / Q3 skill — give-up on weak, fixed at strong

- **Q2** (JA): 「Reyn agent を MCP server として外部から接続するには
  どうすればいい?」
- **Q3** (JA): 「Reyn で新しい skill を作って実行するまでの流れを教えて」
- **V18 weak**: 0/3 each (catalog overlap — `list_actions(category=['mcp.server'])` /
  `list_actions(category=['skill'])` attractors).
- **V18 strong**: 3/3 each — turn 1 goes straight to
  `invoke_action(reyn.source__read, README.md)`.
- **Status**: future-challenge until either (a) the dogfood batch policy
  admits strong-tier scenarios, or (b) a RAG / semantic-router layer
  bridges the catalog overlap.

## Why this is NOT a SP fix-list

We ran 7+ SP variants (V1–V18, ≥60 chain runs) targeting these cases.
Findings:

- SP-side disambiguators (subject-anchor, surface-keyword enumeration,
  imperative-verb clauses) yielded marginal gain on weak (4/18 → 0-4/18
  range) and risked false-positive routing on non-Reyn topics
  (= V13 broke `file__read` on a plain "read this file" task).
- Industry literature ("Tool Selection Problem at Scale",
  vLLM Semantic Router, Tool-to-Agent Retrieval) converges on the same
  conclusion: SP-level disambiguation has a ceiling; catalog overlap
  resolution wants a retrieval / semantic layer, not more prose.

So V18 was chosen as the **clean baseline** (= human-readable,
ambiguous-ask path, no overfit clauses) rather than a deeper SP-side
fight. Catalog overlap is moved out of SP scope into this
future-challenge list.

## When to revisit

- After `action_retrieval.embedding_class` is enabled in the dogfood
  config and `search_actions` enters the hot list (= experimental
  semantic routing).
- After `index_docs` indexes `docs/concepts/*.md` and the chat router
  considers `recall` as a primary entry for self-knowledge queries.
- If a future model class lands that handles catalog-overlap natively.

## References

- SP V18 design: `src/reyn/chat/router_system_prompt.py` (section
  `## Capabilities (routing guide)`).
- Methodology: `scripts/llm_replay.py --chain` (multi-turn chain replay
  with minimal tool executor; built specifically for this investigation).
- Tool description fix shipped alongside V18:
  `src/reyn/tools/read_tool_result.py` (PATH SCOPE clamp — prevents
  weak-LLM confusion between `read_tool_result` and `file__read` on
  source-file paths).
