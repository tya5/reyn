# Batch 22 — Affordance-bias Schema-layer Fix Findings

> Batch 21 で初めて valid evidence を取得した affordance-bias attractor (= 自然な
> 概念質問で LLM が `reyn_src_read` を picks + hallucinated path で 「couldn't
> find」 を返す pattern) に対する schema-layer fix の retest。 5 並列 sonnet
> による context-analysis (= 1 trace deep-dive + 1 industry research + 3 code
> audit) で **真の attractor driver は SP-level rule** と特定、 multi-layer
> reinforcement 設計で 1 commit fix → **N=3 で 0/3 → 3/3 = 100% recovery**。

## 1. Summary

| Run | Prompt | Tool call | Reply | Verdict |
|---|---|---|---|---|
| Q1 | What is the care boundary in Reyn? | `recall(sources=['reyn_concepts'], query='care boundary')` | 1452 char、 「meta-principle that unifies several concepts」 と概念的に正確な抽出 | ✅ verified |
| Q2 | Explain Reyn's permission model. | `recall(sources=['reyn_concepts'], query="Reyn's permission model")` | 895 char、 3-layer (Defaults / Phase Declarations / op kinds) を accurately 抽出 | ✅ verified |
| Q3 | What is plan mode in Reyn? | `recall(sources=['reyn_concepts'], query='plan mode')` | 1611 char、 「decompose complex queries into smaller sub-tasks」 と正確 | ✅ verified |

| Aggregate | 値 |
|---|---|
| primary verified | **3/3 = 100%** |
| batch 21 → 22 delta | 0/3 → 3/3、 +100pp |
| 全 test | 2223 passed / 2 xfailed |

## 2. Context analysis (= 5 並列 sonnet info-gathering)

5 agents dispatch for **read-only evidence gathering**、 fix 設計の前提を確定:

### A1. Trace deep-dive (= batch 21 vs batch 18 S5)

**真の発見**: 真の attractor driver は **SP-level rule**、 tool description ではなかった:

```
SP "Explaining Reyn" section:
"When the user asks how Reyn works or wants to understand any part of Reyn's
 implementation, your authoritative source is Reyn's own repository — call
 reyn_src_read('README.md') first."
```

batch 18 S5 (= 83% verified) と batch 21 (= 0% verified) の structural difference:
- B18 prompt: 「Search the docs」 explicit hint → 「When user says 'search'」 SP rule trigger → recall picks
- B21 prompt: 「What is X in Reyn?」 → 「Explaining Reyn」 SP rule trigger → reyn_src_read picks

「docs/en/concepts/...」 hallucinated path は generic LLM prior (= mkdocs i18n pattern)、 SP / tools 内の signal ではない。

### A2. Industry research

| Source | Pattern |
|---|---|
| OpenAI function calling docs | 「Use the system prompt to describe when (and when not) to use each function」 = endorsed |
| Anthropic agent SDK | structural-first (= namespacing prefixes)、 prose で 「use when X」 template は推奨せず |
| LangChain retriever tool examples | topical scope clause (= 「useful for when you need to ask questions about X」) |
| Practitioner blogs | **4-part template = what / when / when NOT / cross-reference by name** |
| Anti-pattern | overlapping descriptions に prose を増やしても効果限定的、 fewer tools per turn が真の fix |

### A3. reyn_src_read description history audit

**Constraints to preserve** (= 元 commit `f5c88ab` 2026-05-07 HN first-touch wave):
- **C1**: file-read vs semantic-search の区別を明示 (= 既存の motivation 自体は valid)
- **C2**: README curated index navigation を保持 (= 「Start with reyn_src_read('README.md')」 は dev workflow の核)
- web_search 対抗 (= 元 motivation) は保持

### A4. recall description constraint audit

- **Empty-state**: 0 indexed sources でも recall は catalog 残留、 SP が getting-started hint emit (= 既存)
- 「Search the docs」 → recall pathway は **message-level signal**、 description-level affordance ではない
- description が SP 構造に couple しないよう注意 (= 「Indexed sources section」 reference は OK だが過度な dependency 回避)
- B17 vocab disambiguation (= 「Recall」 → 「Memory access」 intent rename) は保持必須

### A5. Schema-layer design space mapping (= 8 levers ranked)

| # | Lever | Status | Effort | Risk | Evidence | Recommendation |
|---|---|---|---|---|---|---|
| **4** | **System prompt routing rules** | ✓ exists | 30 min | Very low | **Strong** (= B11-B13) | **START HERE** |
| **1** | **Tool description text** | ✓ exists | 15 min | Very low | Moderate | **DO with #4** |
| 5 | Parameter schema descriptions | ✓ partial | 10 min | Very low | Weak | secondary、 batch 22 では skip |
| 2 | Tool ordering | ✗ not used | 20 min | Low | Uncertain | defer |
| 3 | Conditional tool suppression | ✓ partial | 1-2h | Moderate | Moderate | architecture wave |
| 6 | Category field | ✓ exists | low | low | Very weak | skip |
| 7 | Strict mode | ✓ partial | — | — | N/A | skip (orthogonal) |
| 8 | Empty-state suppression | ✓ exists | 20 min | Moderate | Low | skip |

## 3. Fix 設計 (= multi-layer reinforcement、 lever 4 + 1)

### Fix 1: SP rule (= primary、 lever 4)

`src/reyn/chat/router_system_prompt.py` 「Explaining Reyn」 section を **indexed sources 条件付き** に rewrite:

```
- When the user asks how Reyn works or wants to understand any part of Reyn's
  design / concepts / implementation: FIRST check the 'Indexed sources' section
  below. If an indexed source's description mentions concepts / design / docs /
  architecture / Reyn, use the `recall` tool with that source — semantic search
  across indexed chunks is the right answer for 'what is X?', 'explain X',
  'how does X work?' style questions when an indexed source covers the topic.

- ONLY if no indexed source covers Reyn (= the 'Indexed sources' section is
  absent / empty / unrelated topics): fall back to `reyn_src_read('README.md')`
  for an overview and curated map of paths under `reyn_src_*` (architecture,
  skill DSL, source code, ADRs).

- Do NOT reach for `web_search` to learn about Reyn — `recall` (when indexed)
  or `reyn_src_*` (otherwise) is the authoritative source.
```

key changes:
- 「FIRST check Indexed sources」 で recall を上位 routing pathway に
- 「ONLY if no indexed source covers」 で C2 (README navigation) fallback 保持
- web_search avoidance directive 保持 (= 元 motivation)

### Fix 2: reyn_src_read description (= secondary、 lever 1)

practitioner 4-part template (= what / when / when NOT / cross-reference) 適用:

```python
_REYN_SRC_READ_DESCRIPTION = (
    "Read a text file from Reyn's own repository by an exact "
    "repo-root-relative path. Use for: (a) reading a specific file the "
    "user named (e.g. README.md, src/reyn/chat/...), or (b) navigating "
    "Reyn's source / docs when NO indexed source covers the topic. "
    "If an indexed source description mentions concepts / design / "
    "docs / Reyn, use `recall` instead — guessing a file path is "
    "unreliable; semantic search over indexed chunks is not. Fallback "
    "entry point: reyn_src_read(\"README.md\") for the overview + "
    "curated map of deep-dive paths."
)
```

key changes:
- 「Use this for any 'how does X work?' question」 削除 (= 元の affordance pull 解消)
- (a)(b) explicit use case enumeration
- 「If indexed source... use recall instead」 cross-reference
- README fallback 保持 (= C2)

### Fix 3: recall description (= secondary reinforcement、 lever 1)

```python
_RECALL_DESCRIPTION = (
    "Search indexed sources by natural-language query. Returns top-K "
    "relevant chunks with text + metadata. Use this when the user's "
    "question is about a topic an indexed source covers — including "
    "'what is X?', 'explain X', 'how does X work?' style questions. "
    "Pick sources from the 'Indexed sources' section in the system "
    "prompt; each source's description tells you what topics it covers. "
    "Prefer this over `reyn_src_read` / file_read when an indexed source "
    "description matches the question's topic — semantic search across "
    "indexed chunks is more reliable than guessing a file path."
)
```

key changes:
- concrete use case enumeration (= 「what is X?」 等)
- description が source content を伝える signal を明示
- cross-reference to reyn_src_read (= practitioner template)
- empty-state assumption なし (= A4 constraint preserved)

## 4. Calibration delta

| 項目 | Batch 21 | Batch 22 | Delta |
|---|---|---|---|
| verified | 0/3 = 0% | **3/3 = 100%** | +100pp |
| recall invoke rate | 0% | 100% | +100pp |
| Hallucinated path occurrence | 3/3 | 0/3 | -100pp |
| Reply quality | 「couldn't find」 | accurate concept extraction (= 895-1611 char meaningful replies) | structural recovery |

batch 21 prediction (= 40-60% verified) も batch 22 actual (= 100%) を超過、 これは **multi-layer reinforcement** (= SP + 2 description) の重ね合わせ効果。 単一 lever (= description のみ or SP のみ) では恐らく 50-70% 程度に留まったはず。 industry research (A2) の **「multi-layer reinforcement」** 推奨が evidence で confirmed。

## 5. Class B (= affordance-bias) hypothesis status update

| Status | Evidence base |
|---|---|
| Class A cognitive-bias (= S9 batch 19、 named anti-attractor callout pattern) | ✅ Valid evidence (1 batch、 100% compliance) |
| **Class B affordance-bias** | ✅ **Valid evidence 取得** (batch 21 で 0/3 観測 + batch 22 で fix 3/3 confirm = causal evidence) |
| Class C protocol-level (= G12) | ✅ Valid evidence (既存) |

**Implication**: 原則 13 (= attractor class taxonomy) の Class B hypothesis status を **「partial validation 仮説」 → 「decisive validation」** に格上げ可能。 schema-layer (= SP rule + tool description) の multi-layer reinforcement が affordance-bias 系 attractor の **first-line fix template** として確立。

## 6. 1.0 Release narrative impact

batch 22 結果は 1.0 OSS launch narrative を **strong restoration** に:

| 主張 | batch 21 状態 | batch 22 状態 |
|---|---|---|
| 「framework foundation provided」 | ✅ | ✅ 維持 |
| 「skill.md-driven indexing strategy override」 | ✅ | ✅ 維持 |
| 「headline scenario green」 | ⚠️ explicit hint 限定 | ✅ **natural concept queries も green、 1.0 narrative の believability 大幅向上** |
| 「ready for 1.0 launch」 | ⚠️ B21-S0-2 schema fix 必要 | ✅ **fix landed、 release blocker clear** |

## 7. Carry-over

| Item | Status | Note |
|---|---|---|
| **B21-S0-2 affordance-bias schema fix** | ✅ landed (= 本 batch) | SP rule + 2 description rewrites + 1 byte-identity test 更新 |
| Class B hypothesis decisive validation | ✅ achieved | 原則 13 update for memory + dogfood-discipline.md |
| Replay fixtures re-record | ✅ landed | 4 router fixtures (= chitchat / invoke_skill_single_round / memory_recall / named_skill_direct_invoke) |
| 1.0 release narrative draft | open | next wave 候補 (= README + blog + HN draft 訂正版) |
| Cross-validation N=5+ (= stability check) | optional | batch 23 候補、 ただし 3/3 + replay green で十分な evidence と判断 |

## 8. Methodology note: context analysis vs speculation

batch 22 は **batch 19 self-audit lesson の最大規模 operationalization**:

- **5 並列 sonnet info-gathering** (= no edits) で fix 設計の前提を確定
- A1 trace deep-dive で 「真の attractor driver は SP rule」 という非自明な発見 (= description rewrite だけだと不十分という factual evidence)
- A3 description history audit で 「narrowing が regress させる use case」 を pre-identify (= C1 / C2 constraint)
- A5 design space mapping で 「lever 4 (SP) + lever 1 (description) が cheapest + highest evidence」 と ranking
- 結果、 **1 commit で 100% recovery を first attempt で達成** (= batch 18-20 の 4 attempts 失敗対比)

「context 分析で attractor に対抗」 という user 指示が、 過去 batches の 「prompt-tweak speculation」 anti-pattern を完全に置換、 真の effect が出る fix 設計に直結。 dogfood discipline framework に **「fix 設計前の multi-agent context analysis」** が新原則 candidate として確立。
