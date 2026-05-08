---
title: category-only SP catalog — landed via the Wave A → revert → G12 fix → retry path
discovered: 2026-05-07
session-context: Wave A revert wave 全体を通した progression の summary。 SP 削除 attempt が 2 度 refuted された後、 真因 G12 Pattern E を分離 fix し、 改めて category-only catalog を landing
related-commits:
  - dc8296f  # Wave A trial (reverted)
  - 589e50f  # Wave A revert
  - aab6be2  # G12 Pattern E envelope workaround
  - 70a9da9  # insights/ infrastructure init
  - f4c5df2  # category-only catalog landing (= 本 insight)
related-giveup: [G12, G23]
related-memory: [feedback_envelope_layer_fix]
status: stable
---

# Category-only SP catalog landing — the Wave A → revert → G12 fix → retry story

## TL;DR

Reyn の chat router system prompt が **真の O(1) catalog scaling** を達成。
skill 数が 10 でも 50 でも 1000 でも SP size 不変 (= 5151 chars)。
業界 standard pattern (= Anthropic Tool Search Tool / OpenAI Tool Search
namespaces / MCP-Zero hierarchical) に整合する形で着地。

到達 path は **2 回失敗 + 1 回根本治療 + 1 回成功** の 4 step:

1. Wave A 削除 attempt (`dc8296f`) → -40pp regression、 revert
2. trigger phrase richer attempt → -50pp regression、 revert
3. **G12 Pattern E 真因判明 + envelope fix (`aab6be2`)** ← root cause cure
4. **category-only retry (`f4c5df2`)** → zero regression、 success

## Context

### 開始時の問題意識

Reyn chat router の SP は skill 数 N に対して O(N) で膨張していた:
- `## Available skills (10)` section が 各 skill の name + truncated description を inline
- 10 skill で ~600 chars、 50 skill で ~3000 chars、 1000 skill では ~60000 chars
- weak LLM (= gemini-2.5-flash-lite) では context bloat が attractor を誘発する worry

業界 trend (= lazy loading / progressive disclosure) と整合させたい motivation
あり、 一方で Reyn 既存設計 (= intent-axis section + per-category list_*) が
どの程度業界 practice と一致 / 逸脱しているか不明。

### 業界 practice 調査 (= 詳細は別 insight)

[industry tool discovery patterns survey](2026-05-07-industry-tool-discovery-survey.md)
で 5 source (Anthropic / OpenAI / Tool RAG / MCP-Zero / LangChain) を synthesis、
2 派分類 + Reyn 適用判断を整理。 結論: 業界は **「category 層を SP に残す、 item
層は lazy」** で converge、 Reyn 既存 design は派 (a) Anthropic 派の simplified
version に近い。

## Phase 1 — Wave A 削除 attempt (失敗)

### Hypothesis
intent-axis section (= 58 行 / 700 chars) が冗長、 削除して `discover_tools`
meta-tool に置換すれば 12% prompt 削減 + 業界 lazy 化。

### Implementation (`dc8296f`)
- `## What you can do (intent axis)` section を削除
- `discover_tools` tool 追加 (= on-demand grouped catalog)
- Behaviour rule に soft pointer
- 1271 → 1275 tests pass、 4 LLMReplay fixture re-record、 land

### Dogfood verify (= 真の dogfood、 N=10)

W1 V3 ABSOLUTE rule scenario (`skill_improver で word_stats_demo を review して`):
- pre-Wave-A: ~80% invoke success
- post-Wave-A: **40%** (= **-40pp regression**)
- Gemini `<ctrl42>` format leak: 20% → **60%** (= 倍増)

→ revert (`589e50f`)。

### 学び
- 「prompt size 圧縮 = 純粋利得」 は誤り、 char count ではなく **signal density** で評価
- 「intent-axis section は load-bearing routing scaffold」 当初仮説 → ただし因果関係は次 phase で覆る
- giveup-tracker G23 として記録

## Phase 2 — trigger phrase richer attempt (失敗)

### Hypothesis
Wave A は SP 削減 *方向* が悪かった、 逆に **「category routing trigger phrases
を Behaviour に追加」** で routing decision の structural cue 強化すれば改善する。

### Implementation
- `## Available skills` を維持 (= W1 anchor 死守)
- Behaviour に「Skills 探す: 「<verb> して」」 「Agents 探す: 「ask the X」」
  等の category routing trigger 追加 (+958 chars、 SP 6120 chars に膨張)

### Dogfood verify (N=10)

| scenario | baseline | trigger 追加 | Δ |
|---|---|---|---|
| W1 invoke V3 | ~80% | **30%** | -50pp ✗✗ |
| W7 unnamed task (新) | n/a | **10%** | new ✗ (= G12 暴露) |

W7 で「list_skills を call したが turn 2 で empty stop」 が 8/10 観察。 → 真因
発見: **G12 Pattern E (= post-tool empty-stop attractor)** が list_skills 後にも
発火、 SP 内介入は (削除 / 追加 問わず) attractor を re-shuffle して regression。

→ revert (working tree のみ、 commit せず)。

### 学び
- 「trigger phrase richer で routing 改善」 仮説 refuted
- LLM は procedural rule で reasoning してない、 **attractor で動く**
- SP 構造変化はどんな方向でも weak LLM で regression risk
- 真の root cause は SP 内容ではなく **envelope 構造 (= G12 Pattern E)**

## Phase 3 — G12 Pattern E envelope fix (root cause cure)

### Mutation isolation methodology

[envelope-layer attractor fix insight](2026-05-07-envelope-layer-attractor-fix.md)
で詳述。 V0-V11 の 1 軸 mutation で **`(answered)` trailing user message inject**
が:
- empty_stop 0% (= 完全消滅)
- 100% text reply
- 0% duplicate tool_call (= 副作用なし)
- multi-tool chain freedom 保持

### Implementation (`aab6be2`)
2 行 envelope-layer 介入:

```python
# src/reyn/llm/llm.py:call_llm_tools
if messages and isinstance(messages[-1], dict) and messages[-1].get("role") == "tool":
    messages = messages + [{"role": "user", "content": "(answered)"}]
```

= SP 一切 touch せず、 history persistence にも影響なし、 chat history は inject
前のまま clean。

### Dogfood verify

| scenario | baseline | envelope fix | Δ |
|---|---|---|---|
| W1 invoke V3 | ~80% | 90% | +10pp ✓ |
| W2 web_search | ~90% | 100% | +10pp ✓ |
| W5 remember (G12 直撃) | 30-50% | 100% | +50pp ✓✓ |
| W7 unnamed task (G12) | 10% | 70% | +60pp ✓✓ |

→ G12 Pattern E **完全消滅**。 SP 削減 retry の前提整備完了。

## Phase 4 — category-only retry (成功)

### Hypothesis
G12 Pattern E が解消した状態で、 改めて category-only catalog を試せば、 W7 で
発火していた G12 list_skills attractor が消えてるので、 真の効果が測れる。 業界
practice (= MCP-Zero hierarchical) と整合する形で SP O(1) scaling 達成可能。

### Implementation (`f4c5df2`)

SP 変更:
- `## Available skills (N)` の inline list (= name + truncated desc × N) を
  **`## Skills (N available) — call list_skills(path) to browse...` の 3 行 pointer に置換**
- Hallucination defense は schema enum (= invoke_skill `name` field の enum
  constraint) に **完全 delegate** (= 元々 structural defense として持っていた)

Behaviour rule rewrite:
- 「skill 名が Available skills list にあれば」 → 「skill 名が **invoke_skill enum**
  の valid value なら」
- ROUTING RULE (ABSOLUTE) も enum 参照に変更、 example 文言保持

Memory section は inline 維持 (= user 設計判断、 小 N stable な前提なら inline
が recall 1-turn 短縮で OK)。

### Dogfood verify (N=10 each)

| scenario | envelope-only baseline | + category-only retry | Δ |
|---|---|---|---|
| W1 invoke V3 | 90% | **90%** | 0 ✓ baseline 死守 |
| W2 web_search | 100% | **100%** | 0 ✓ |
| W3 chitchat | 100% | **100%** | 0 ✓ |
| W4 capabilities | 100% | **100%** | 0 ✓ |
| W5 remember (G12) | 100% | **100%** | 0 ✓ envelope fix 維持 |
| W6 recall query | 100% | **100%** | 0 ✓ |
| W7 unnamed task | 70% | **70%** | 0 ✓ |

= **zero regression、 全 scenario healthy**。

### O(1) SP size scaling (= 真の win)

| skill count | SP size (= 元 design) | SP size (= category-only) |
|---|---|---|
| 10 | 5162 chars | 5151 chars |
| 50 | ~7000 chars (推定) | **5151 chars** |
| 100 | ~12000 chars (推定) | **5151 chars** |
| 1000 | ~60000 chars (推定) | **5151 chars** |

10 skills 時の char 削減は微小だが、 asymptotic な「scale しても膨張しない」
性質が headline value。 Reyn が marketplace / OSS 化で skill 1000+ になっても
SP は constant。

## 全 phase の集約教訓

### 1. 介入 layer の優先順位 (= care boundary 整合)

| layer | 例 | 介入 cost | 副作用 risk |
|---|---|---|---|
| 1. envelope / protocol | role 遷移、 message 順序、 必須 field | 低 (= 数行 code) | 低 (= protocol-defined surface) |
| 2. schema / tool definition | enum constraint、 parameter schema | 中 | 低 |
| 3. system prompt content | rule wording、 ordering、 inline list | 低 | **高** (= attractor re-shuffle) |

attractor 系 LLM 挙動問題は **下 layer から疑う**。 SP は最終手段。

### 2. 真因切り分けの discipline

「観測した bug ≠ 真の bug」 pattern (= dogfood-discipline 6 番目「reproduce-first」):
- Phase 1 で「intent-axis 削除が W1 を壊した」 と観測
- Phase 2 で「trigger 追加でも壊れる」 ことから「方向の問題ではない」 と再考
- Phase 3 で「W7 G12 暴露」 から真因 G12 Pattern E に到達
- Phase 4 で envelope fix 後に SP 削除 retry → success

= **観測した症状 (= W1 -40pp) は真因 (= G12 Pattern E) の indirect symptom**。
真因を解かずに symptom layer (= SP) で治療しようとすると attractor re-shuffle で
副作用。

### 3. 業界 practice 調査の value

調査前は「Reyn 設計が業界から逸脱してるか」 不明、 trial-and-error で path 探索
していた。 5 source 調査後 (= 派 (a) Anthropic 派 vs 派 (b) Tool RAG 派 の分類)、
**Reyn は派 (a) simplified version を既に持っている** と判明し、 retry path が
明確化。 「自前で path を発明する」 vs 「業界 path に着地」 の意思決定は調査
ベースで。

### 4. multi-perspective review の funnel

数字に踊らされない discipline:
- 同 scenario でも測定経路 (= subprocess vs programmatic) で 50pp 振れる事実
- N=10 noise window ±10-20pp 大きい、 N=50+ で安定推定
- 1 経路 measurement で結論せず multi-path verify
- mutation isolation で「何が effect 持つか」 matrix 化

### 5. structural fix の reusability

`(answered)` envelope inject pattern は:
- workaround (= provider quirk への adapter) と認識、 model 改善で obsolete 候補
- ただし pattern 自体は **真因が envelope 層にある時の universal recipe**
- code に明示 comment (= future contributor の remove 判断 path 残す)

## 適用可能性 (= future work hint)

### 同 pattern が活きそうな場面

1. weak LLM での tool_use protocol attractor 一般 (= post-tool empty stop、
   parallel call confusion 等)
2. multi-tool chain で「途中 tool」 の hallucination が発生する場面 (= envelope
   structure で next-step cue 強化)
3. catalog の真の O(1) scaling が必要になる場面 (= marketplace / 1000+ tools)

### 不適用な場面

1. SP 内 instruction の意味的曖昧さ → SP rewriting layer の問題
2. tool description の不正確さ → tool definition layer の問題
3. 真の LLM 能力不足 → 強モデル移行が真の解

## References

### 同 session の sibling insights
- [envelope-layer attractor fix + mutation isolation methodology](2026-05-07-envelope-layer-attractor-fix.md)
- [industry tool discovery patterns survey](2026-05-07-industry-tool-discovery-survey.md)

### 関連 giveup-tracker entries
- [G12: attractor variant family (Pattern E section)](../dogfood/giveup-tracker.md#g12)
- [G23: intent-axis section is load-bearing routing scaffold](../dogfood/giveup-tracker.md#g23)

### Commits
- `dc8296f` Wave A trial (= reverted)
- `589e50f` Wave A revert
- `aab6be2` G12 Pattern E envelope workaround
- `f4c5df2` category-only catalog landing (= 本 insight が記録する成果)
