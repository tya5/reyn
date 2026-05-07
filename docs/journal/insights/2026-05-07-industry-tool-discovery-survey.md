---
title: industry tool discovery patterns — Anthropic / OpenAI / Tool RAG / MCP-Zero / LangChain
discovered: 2026-05-07
session-context: Wave A revert wave で「lazy loading 路線は業界で確立してるか？」 「top-level intent → category routing は業界で解決済か？」 を WebSearch / WebFetch で調査した synthesis
related-commits:
  - 4bd24b7  # giveup-tracker G23 update with industry references
related-giveup: [G23]
related-memory: [feedback_envelope_layer_fix]
status: stable
---

# Industry tool discovery patterns survey (2026-05)

## TL;DR

LLM agent の tool 数が増えた時、 **「全 tool description を SP に inline する」
は誰もやってない**。 業界は 2 派に分かれて scale 解を実装:

- **派 (a) LLM-driven meta-tool**: 専用 search tool を SP に常駐、 LLM が
  自発的に呼ぶ。 Anthropic / OpenAI の official solution。
- **派 (b) system-driven retrieval**: query → semantic search → top-k tool
  を LLM 前で filter。 academic + Tool RAG / MCP-Zero。

Reyn vision (= P3: LLM が decision、 OS が runtime) との整合性は (a) が高い。
Reyn 既存 design の `intent-axis section + per-category list_*` family は
(a) 派の **simplified version** に既に近い。

## なぜこの調査が必要だったか

Wave A 失敗 (= intent-axis 削除 → -40pp regression) を受けて user 発言:

> 「lazy loading は業界の流れであって、 人間の思考とも適合しているのです。
> これによって常時発生するコンテキストを軽くすることで、 本当に大事なことを
> 考える余裕を生む。」

「Reyn が業界 practice から逸脱してるのか / Reyn 設計が unique なのか」 を
評価するため、 5 source を調べて synthesis。

## 派 (a) LLM-driven meta-tool

### Anthropic Agent Skills (= Reyn と最類似)

3-level progressive disclosure:

| Level | 内容 | いつ load | コスト |
|---|---|---|---|
| **L1: Metadata** (= name + description) | YAML frontmatter | **常に system prompt に inline** | ~100 tokens / skill (= 50 skill で ~5000 tokens) |
| L2: Instructions | SKILL.md body | trigger 時 bash read | < 5k tokens |
| L3: Resources | 追加 file | 必要時 file access | 無制限 |

L1 description の Anthropic 公式例:

```yaml
description: Extract text and tables from PDF files, fill forms, merge documents.
  Use when working with PDF files or when the user mentions PDFs, forms,
  or document extraction.
```

→ description は **「何をする」 + 「いつ呼ぶ」 (= trigger phrases)** を兼ねる。
max 1024 chars。 これが LLM の routing decision の primary cue。

[platform.claude.com/docs/en/agents-and-tools/agent-skills/overview](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)

### Anthropic Tool Search Tool (= 補助 meta-tool)

Skill とは別に、 全 tool 横断 search の専用 tool:

- 全 tool 定義を API に渡すが、 `defer_loading: true` flag で discoverable on-demand 化
- LLM は **Tool Search Tool itself + critical tools (= `defer_loading: false`)** だけ常駐
- LLM が自発的に `tool_search(query)` を呼んで regex / BM25 で全 tool 検索
- benchmark: **85% token reduction** (122,800 → 191,300 tokens preserved)

[platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)

### OpenAI Tool Search (= GPT-5.4+)

namespaces / MCP server 単位の lazy loading:

> "the model sees only the namespace or server name and description at the
> beginning, without showing details of the individual functions contained
> within it until the tool search tool loads them."

= **namespace 名 + description のみ upfront**、 中の function は lazy。 これが
**namespace = category** の standard interpretation。 個別 function 単位でも
`defer_loading` flag で同様に。

2 模式:
1. **Hosted tool search**: API が search 実行
2. **Client-executed tool search**: app 側 backend で discovery (= project /
   tenant state 反映可)

[developers.openai.com/api/docs/guides/tools-tool-search](https://developers.openai.com/api/docs/guides/tools-tool-search)

### LangChain dynamic tools

- pre-register all tools、 runtime で subset 露出 (= state / permission /
  conversation stage 等で filter)
- tool description quality が selection accuracy に直結
- few-shot examples が大幅効果: zero-shot 16% → few-shot (3 examples) **52%** on Claude 3 Sonnet

[blog.langchain.com/few-shot-prompting-to-improve-tool-calling-performance/](https://blog.langchain.com/few-shot-prompting-to-improve-tool-calling-performance/)

## 派 (b) system-driven retrieval

### Tool RAG / RAG-MCP (= academic + 産業)

query → 全 tool description DB を semantic search → top-k inject:

- LLM 前で system 側 filter
- benchmark: tool invocation accuracy **3 倍**、 prompt length **半減**
- **Anthropic RAG-MCP**: 13% → **43% accuracy** (= 大規模 toolset)、 prompt size 大幅削減

[next.redhat.com/2025/11/26/tool-rag-the-next-breakthrough-in-scalable-ai-agents/](https://next.redhat.com/2025/11/26/tool-rag-the-next-breakthrough-in-scalable-ai-agents/)

### MCP-Zero — hierarchical 2-stage routing

```
query → server-level filtering (= server description match) → tool-level ranking
```

- **2-stage** semantic alignment
- benchmark: **98% token reduction** on APIBank、 ~3k candidate tools / 248.1k tokens で accuracy 維持
- multi-turn でも tool ecosystem 拡大に scale 一定

[arxiv.org/abs/2506.01056](https://arxiv.org/abs/2506.01056)

### ToolLLM — 3-tier hierarchy

RapidAPI tree 構造活用:
- **domain → category → individual API** の 3 tier
- DFS-based Decision Tree で navigate
- 最も「人間設計の hierarchy」 寄りの practice

## 2 派の比較

| 軸 | (a) LLM-driven (Anthropic / OpenAI) | (b) System-driven (Tool RAG / MCP-Zero) |
|---|---|---|
| who decides | LLM が自発 | system が事前 filter |
| infra cost | 低 (= meta-tool 1 つ) | 高 (= embedding pipeline + vector DB) |
| weak LLM 信頼性 | △ 「いつ search 呼ぶか」 が prompt 依存 | ✓ system 側 deterministic |
| reasoning transparency | ✓ LLM の判断可視 | △ retrieval が opaque |
| Reyn vision (= P3) 整合 | ✓ LLM が decision | △ 部分的不整合 (= system が pre-filter は OS judgment) |
| 小 N (~30 tools) での効果 | over-engineering | over-engineering |
| 大 N (~100+ tools) | sweet spot | sweet spot |
| 大 N (~1000+ tools) | scale 余地あり | mainstream |

## Top-level intent → category routing — 業界の hard problem

「user input → どの category」 の上位 routing は **業界も完全には解いてない**。
解は 2 派の中間 / hybrid:

### 派 (a) の答え: 専用 meta-tool

- Anthropic Tool Search Tool = まさに **補助 tool** として実装
- LLM が「困ったら search 呼ぶ」 という mental model
- search 自体は regex / BM25 (= keyword)、 embedding ではない
- weak LLM での「いつ呼ぶか」 判断が信頼性 risk

### 派 (b) の答え: 階層 semantic search

- MCP-Zero hierarchical = server-level → tool-level
- system が query embedding で各層を filter
- LLM は filtered subset のみ見る (= 真の lazy)

### 中間: trigger-rich descriptions

LangChain few-shot evidence + Anthropic L1 metadata pattern が示唆:
- description に **「Use when ...」 trigger phrases** 含めれば LLM が pattern
  match で route 可能
- few-shot examples (3 件) で accuracy 16% → 52% jump
- = description 設計次第で SP-level routing も成立する

## Reyn 既存 design との対応

Reyn の `intent-axis section + per-category list_*` family は (a) 派の
**simplified version** に既に近い:

| Anthropic (Tool Search Tool) | Reyn 既存 |
|---|---|
| Tool Search Tool (= 1 つの meta) | `list_skills` / `list_agents` / `list_memory` / `list_mcp_*` (= category 別 5+ tool) |
| `defer_loading=false` で常駐 tool | `invoke_skill` / `web_search` 等の action tool |
| BM25 / regex search query | path-based browse (= `list_skills(category)`) |
| inline tool descriptions (= L1) | inline category descriptions (= intent-axis section) |
| 1 universal meta + tag-based filter | category 別 list_* tool 群 |

→ **Reyn は (a) 派の縮約版を実装済**。 ただし違いも:

- Reyn は tag-based filter (= category 構造) が tool 化されてる、 Anthropic は
  1 universal search に集約
- Reyn は all category eager に SP に書く、 Anthropic は 1 entry-point から
  navigate
- Reyn は category 構造を P7 (OS skill-agnostic) で fix、 Anthropic は user-
  defined skill metadata に依存

## Reyn の現実的な path

### 小 N (= 現状 ~30 skills)

- (a) (b) どちらも over-engineering
- 既存 design (= intent-axis + per-category list_*) で十分
- improvement 余地は **inline description quality** (= 「Use when ...」 trigger
  phrases 強化)

### 中 N (~100 skills)

- (a) 派 path: `## Available skills` の inline list を **name-only** に絞る
  + description は `list_skills` 経由で fetch (= L1 metadata 削減)
- 業界 evidence: 100 件規模なら category 名 + name のみで十分判別可、
  description は trigger 時 fetch
- ただし **G12 Pattern E (= post-tool empty-stop)** が下にいる限り、 list_skills
  経由が増えると attractor 暴露面積も増える → envelope fix `aab6be2` で前提整備済

### 大 N (~1000 skills)

- (a) 派: Anthropic Tool Search Tool 移植 (= regex / BM25 search、 全 tool
  inline 不要)
- (b) 派: Tool RAG / MCP-Zero pattern (= embedding pipeline)
- どちらも infra investment 必要、 trigger 条件が出てから着手

## Reyn 適用判断

### 当面 (= ~30 skills)

- **何もしない**: 既存 design で routing accuracy 十分 (= W1 80%+ baseline)
- 後続 wave で **description quality 改善** だけ chase (= "Use when ..."
  phrasing、 inline 維持で trigger richness のみ強化)

### 中期 (~100 skills、 user growth で trigger)

- **category-only catalog 化**: `## Available skills` を 1 行 pointer に置換、
  inline list は維持しない
- G12 envelope fix が下にあれば list_skills 増加を吸収可能
- N≥10 dogfood で W1 baseline 死守 + 大 N simulate scenario 追加 verify

### 長期 (= ~1000 skills、 marketplace etc.)

- **Tool Search Tool migration** or **Tool RAG infra**: trigger 条件が出てから
  選定
- choice は Reyn vision (= predictability over autonomy) と整合確認、 RAG は
  retrieval 可視性が課題

## References

### Source URLs

- [Agent Skills - Claude API Docs](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)
- [Tool search tool - Claude API Docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)
- [Anthropic's Tool Search Tool - AI Engineer Guide](https://aiengineerguide.com/til/anthropic-tool-search-tool/)
- [Tool search | OpenAI API](https://developers.openai.com/api/docs/guides/tools-tool-search)
- [Tool RAG - Red Hat](https://next.redhat.com/2025/11/26/tool-rag-the-next-breakthrough-in-scalable-ai-agents/)
- [MCP-Zero (arxiv 2506.01056)](https://arxiv.org/abs/2506.01056)
- [Few-shot prompting - LangChain Blog](https://blog.langchain.com/few-shot-prompting-to-improve-tool-calling-performance/)
- [Tool-to-Agent Retrieval (arxiv 2511.01854)](https://arxiv.org/pdf/2511.01854)
- [Instruction-Tool Retrieval ITR (arxiv 2602.17046)](https://arxiv.org/html/2602.17046)

### 関連 Reyn doc

- [G23: intent-axis section is load-bearing routing scaffold](../dogfood/giveup-tracker.md#g23) (= 同 session の Wave A revert evidence)
- [envelope-layer attractor fix + mutation isolation methodology](2026-05-07-envelope-layer-attractor-fix.md) (= 同 session の sibling insight)
- [Reyn vision project memory](../../../memory/project_reyn_vision.md) (= P3 + predictability over autonomy framing)
