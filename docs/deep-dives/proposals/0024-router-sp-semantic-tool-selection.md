# FP-0024: Router — Semantic Tool Selection

**Status**: proposed
**Proposed**: 2026-05-13
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

Introduce a semantic / hybrid retrieval layer between user input and
`invoke_skill` selection. Rather than always passing the full skill enum
to the LLM, use BM25 or embedding similarity to pre-filter the catalog to
the top-K candidates, then ask the LLM to choose among those. Addresses the
`invoke_skill.name` enum O(N_skills) schema bloat and improves routing
precision for ambiguous inputs.

---

## Motivation

### Current routing path

```
User input
  → LLM sees full invoke_skill.name enum (all N skill names)
  → LLM picks one (or calls list_skills first)
```

This works well today (~100–200 skills). Industry research identifies two
failure modes as catalog size grows:

**1. Schema enum bloat**
`invoke_skill.name` lists all skill names in the tool schema. At N=1000 skills
the enum alone consumes ~5k–8k tokens. Each session pays this cost; prompt
cache misses increase because the enum changes when skills are added.
*Source: estimated from avg 20 chars/name × 1000 = 20k chars ≈ 5k–8k tokens.*

**2. Decision fatigue with large enums**
Anthropic research: tool selection accuracy degrades significantly beyond
30–50 tools in context. OpenAI's official guidance: "Aim for fewer than 20
functions at the start of a turn."
*Source: Anthropic "Advanced Tool Use" (2025); OpenAI Function Calling docs.*

### What research recommends

| Approach | Accuracy improvement | Latency | Production readiness |
|---|---|---|---|
| BM25 keyword search | ++ | Low | GA (Anthropic tool_search_tool) |
| Embedding cosine similarity | + | Medium | Prototype stage |
| Hybrid (BM25 + embedding) | +++ | Medium | Best in research |
| OATS (outcome-aware embeddings) | +++ | Low | Research stage |

Key finding from Dynamic ReAct (arxiv 2509.20386): "Search and Load" meta-tools
cut tool loading by 50% while maintaining or improving precision.

Key finding from Tool2Vec (Red Hat, 2025): embedding the *questions a tool can
answer* (not just the description) yields ~50% relative improvement in Recall@5
vs. embedding the description alone. This closes the gap between developer
vocabulary and user vocabulary.

Key finding from OATS (arxiv 2603.13426): a static similarity lookup
(pre-computed, no GPU at serving time) achieves NDCG@5 0.940 vs 0.869 baseline
at 1000x lower latency than LLM-based selection.

---

## Proposed implementation

Four components, implemented in order (A → B → C → D).

### Component A — BM25 skill pre-filter (SMALL)

**What**: Before passing skills to the LLM, run a BM25 search over skill names
+ descriptions using the user message as the query. Pass only the top-K (default
K=5) to `invoke_skill.name` enum.

**Where**: `src/reyn/chat/router_loop.py` — before `build_tools()` call.

```python
# Before tool build: narrow skill list with BM25
if len(all_skills) > SKILL_SEARCH_THRESHOLD:  # e.g. 20
    candidate_skills = bm25_skill_search(user_message, all_skills, top_k=5)
else:
    candidate_skills = all_skills

tools = build_tools(..., available_skills=candidate_skills, ...)
```

**BM25 index**: built once per session on first message; rebuilt when skill
registry changes. Lives in `src/reyn/chat/services/skill_search.py` (new).

**Fallback**: if BM25 returns 0 results (no keyword match), fall through to
full enum (existing behavior).

**Impact on prompt**: `invoke_skill.name` enum shrinks from O(N_skills) to O(K).
Cache hit rate for tool schema increases.

### Component B — Skill description enrichment / Tool2Vec (MEDIUM)

**What**: Augment each skill's search-facing description with a set of
*example questions the skill can answer*, generated offline by a light LLM
call. Store as `skill.search_hints` in the skill registry.

```yaml
# skill.md frontmatter (new optional field)
search_hints:
  - "review this code and suggest improvements"
  - "check my pull request for issues"
  - "audit the security of this file"
```

If `search_hints` is absent, fall back to the existing `description` field.

**Impact**: BM25 and embedding search operate over richer text, closing the
developer-vocabulary / user-vocabulary gap identified in Tool2Vec.

**Who generates hints**: Optionally, `reyn skill enrich <name>` CLI command
runs a one-shot LLM call to generate hints and writes them to the frontmatter.
Skill authors can also write hints manually.

### Component C — Embedding-based pre-filter (MEDIUM)

**What**: Replace or supplement BM25 (Component A) with a vector similarity
search. Embed each skill's `description + search_hints` offline; at query time,
embed the user message and return top-K by cosine similarity.

**Where**: `src/reyn/chat/services/skill_search.py` — `SkillSearchIndex` class
with two backends:
- `BM25Backend` (Component A)
- `EmbeddingBackend` (Component C)
- `HybridBackend` (BM25 + embedding, RRF fusion)

**Embedding model**: configurable in `reyn.yaml`:

```yaml
skill_search:
  backend: hybrid          # bm25 | embedding | hybrid
  embedding_model: local   # local (sentence-transformers) | api (openai/anthropic)
  top_k: 5
```

Default `backend: bm25` (no embedding model required). Operators opt in to
`embedding` or `hybrid` explicitly.

**Index lifecycle**:
- Built once at session start; stored in `.reyn/skill-index/` (gitignored)
- Rebuilt when skill files change (file watcher or explicit `reyn skill reindex`)
- Embedding freshness managed by skill file hash comparison

### Component D — Anthropic tool_search_tool integration (MEDIUM)

**What**: For MCP-heavy deployments with 30+ MCP tools, use Anthropic's
server-side `tool_search_tool` (GA as of 2025-11) with `defer_loading: true`
instead of passing all MCP tool schemas upfront.

**Where**: `src/reyn/chat/router_tools.py` — `build_tools()`.

```python
if mcp_tool_count > MCP_SEARCH_THRESHOLD:  # e.g. 30
    # Include only the search meta-tool; individual tools loaded on demand
    tools.append(build_mcp_search_tool(mcp_servers))
else:
    # Existing behavior: inline all MCP tools
    tools.extend(build_mcp_tools(mcp_servers))
```

**Impact**: For large MCP deployments, reduces context from O(N_mcp_tools) to
O(1) (search tool only + K=3–5 results per search). Spring AI experiment: 63–64%
token reduction with Anthropic backend.

---

## Target files

| File | Change |
|---|---|
| `src/reyn/chat/router_loop.py` | Pre-filter `available_skills` before `build_tools()` |
| `src/reyn/chat/router_tools.py` | Pass narrowed skill list; Component D MCP search |
| `src/reyn/chat/services/skill_search.py` | New: `SkillSearchIndex` (BM25 + embedding backends) |
| `src/reyn/config.py` | New: `SkillSearchConfig` (backend, top_k, embedding_model) |
| `src/reyn/cli/skill.py` | New subcommand: `reyn skill enrich` (Component B) |
| `docs/concepts/permission-model.md` | Update skill routing section |

---

## Dependencies

- Component A depends on nothing; can ship independently
- Component B depends on nothing; can ship independently (enriches A/C)
- Component C depends on Component A (replaces BM25 backend)
- Component D depends on nothing; can ship independently

All components are additive and opt-in. Default config preserves existing
behavior (no search, full enum — same as today).

---

## Cost estimate

| Component | Task | Cost |
|---|---|---|
| A | BM25 pre-filter + `SkillSearchIndex` (BM25 backend) | SMALL |
| B | `search_hints` frontmatter field + `reyn skill enrich` CLI | SMALL |
| C | Embedding backend + hybrid + `.reyn/skill-index/` lifecycle | MEDIUM |
| D | Anthropic tool_search_tool MCP integration | SMALL |
| Config + docs | `SkillSearchConfig` + reyn.yaml docs | SMALL |
| **Total** | | **MEDIUM** |

A + B can ship before C and deliver measurable improvement. C is the largest
investment but unlocks Tool2Vec-level recall gains.

---

## Verification

1. **Component A**: With 50+ skills and BM25 enabled, `invoke_skill.name` enum
   in the tool schema contains ≤ K entries per turn. Confirm no regression on
   existing skill routing dogfood.
2. **Component B**: `reyn skill enrich review` writes `search_hints:` to
   `skill.md` frontmatter. BM25 and embedding search use hints in index.
3. **Component C**: Embed 50 skills; query "review my PR" → top-5 includes
   `code_review` skill. Recall@5 ≥ 80%.
4. **Component D**: With 40+ MCP tools, tool schema sent to LLM contains only
   the search meta-tool (not all 40). After a tool search call, the correct
   MCP tool is loaded.
5. **Token reduction**: Measure `input_tokens` (prompt cache miss) before/after
   Component A. Expect 30–50% reduction for large catalogs.

---

## Related

- FP-0023 (`0023-router-sp-quick-wins.md`) — prerequisite quick wins
- Dynamic ReAct (arxiv 2509.20386) — "Search and Load" pattern
- Tool2Vec (Red Hat, 2025) — usage-driven embedding
- OATS (arxiv 2603.13426) — outcome-aware tool selection
- Anthropic tool_search_tool docs — `defer_loading` + BM25/regex backends
- langgraph-bigtool — LangChain's 50+ tool pattern
- Spring AI `ToolSearchToolCallAdvisor` — 63% token reduction with Anthropic
