---
title: envelope-layer attractor fix + mutation isolation methodology
discovered: 2026-05-07
session-context: Wave A revert wave — SP-layer 2 件の attempt (= intent-axis 削除 + trigger phrase 追加) が共に -40〜-50pp regression を引き起こし、 真因が SP 内容ではなく envelope 構造にあると判明
related-commits:
  - aab6be2  # G12 Pattern E envelope workaround
  - 4bd24b7  # giveup-tracker G12 / G23 update
  - 589e50f  # Wave A revert
  - dc8296f  # Wave A trial (= reverted)
related-giveup: [G12, G23]
related-memory: [feedback_envelope_layer_fix]
status: stable
---

# Envelope-layer attractor fix + mutation isolation methodology

## TL;DR

LLM 挙動問題で system prompt を弄っても N≥10 で数字が wide range に振れる
(= 30%-100% 等) 場合、 真因は **protocol envelope の構造** (= role 遷移 /
message 順序 / 必須 field) にある可能性。 SP 介入は attractor を re-shuffle
するだけで真の fix にならない。

介入 layer の優先順位 (= care boundary 整合):

1. **envelope / protocol layer** (= P3 + pre-call structural environment = OS responsibility)
2. **schema / tool definition layer** (= structural defense)
3. **system prompt content layer** (= weak、 attractor re-shuffle risk)

## Background — G12 Pattern E observation

`gemini-2.5-flash-lite` via LiteLLM proxy (= OpenAI tool_use compat path) が
`role=tool` 受信後の turn で **0 completion tokens / finish_reason=stop** の
empty-stop attractor を 30-100% range で発火 (= 確率的、 deterministic-leaning、
context details で振れる)。

具体例 (= W5 scenario):

```
user:   "remember that I prefer Python 3.12 for new projects"
asst:   tool_calls=[remember_shared(...)]
tool:   {"status": "ok", "data": {...}}
asst:   ← LLM が完全 empty で stop。 user は何も応答を見れない
```

remember_shared は呼ばれて memory には書かれるが、 user 視点では「沈黙」 で
broken UX。 list_skills など他 tool 後でも同じ pattern (= W7 で 80% repro)。

## Mutation isolation methodology — V0 から始める debug 手順

attractor の cause を絞り込むには **1 軸ずつ mutate して effect 測定**。
session 中で適用した手順:

### Step 1 — baseline pin (V0)

実 trace から messages を loaded、 N=10 で repro 性能確定:

```python
# /tmp/wave_a_min_context.py から抜粋
req = _load_turn2(TRACE)  # 実 reyn chat trace
base_messages = req["messages"]
base_tools = req["tools"]
_shot(base_messages, base_tools, "V0 baseline (full Wave A turn 2)")
# → V0: empty_stop=10/10 (100%)
```

### Step 2 — single-axis mutation, N=10 each

各 V<n> で **1 軸だけ変更**、 baseline からの delta で「何が effect 持つか」 を
特定:

| ID | mutation | effect (vs V0) | 解釈 |
|---|---|---|---|
| V0 | (none) | 100% empty | baseline |
| V1 | tool_result content に nudge 追記 | 100% empty (= 効かず) | tool_result 内容は無関係 |
| V2 | SP の ABSOLUTE rule strip | 100% empty | rule 内容は無関係 |
| V3 | tools= から 1 tool 除外 | 80% empty (-20pp) | tools array 構成が一部寄与 |
| V4 | minimal SP (= 大幅圧縮) | **0% empty** | SP size が effect 大 |

### Step 3 — confound 分離 (= V4 の「brevity vs explicit-order」)

V4 が極端に変わった (= 100→0%)、 ただし V4 は「brevity (= SP 圧縮)」 と「explicit
post-tool order (= 末尾に "Now confirm to user.")」 の 2 軸を同時に変えてた。
isolation 必要:

| ID | mutation | effect | 結論 |
|---|---|---|---|
| V5 | minimal SP **without** confirmation order | 0% empty | brevity 単独で十分 |
| V6 | full SP + rule **at end** | 0% empty | rule 単独 (= 末尾位置) で十分 |
| V7 | full SP + rule **mid-Behaviour** | 40% empty | rule の **位置効く** (= 末尾 only effective) |

= 「brevity と end-position rule が独立に effective、 mid-position rule は弱い」。
weak LLM の attention pattern (= recency bias) と整合。

### Step 4 — envelope-layer 仮説検証

別 wave (= `/tmp/wave_a_trailing_user.py`) で envelope 側の mutation:

| ID | trailing user message | empty_stop | reply | tool_call (= 副作用) |
|---|---|---|---|---|
| V0 | (none) | 30-60% | 40-70% | 0% |
| V1 | `"(continue)"` | **0%** | 0% | **70-100% (= duplicate)** |
| V2 | `"Please respond — text or tool"` | 0% | 70% | 30% (= 副作用 risk) |
| V4 | (= 元 user query repeat) | 0% | 100% | 0% |
| **V7** | `"(answered)"` | **0%** | **100%** | **0%** |
| V8 | `"Now confirm."` | 0% | 0% | 100% (= duplicate) |
| V9 | `"thanks"` | 0% | 100% | 0% (= "You're welcome!" UX 違反) |

→ envelope 層 (= role=tool 後に role=user inject) で 0% empty 達成可、
**(answered)** が neutral state signal として最 clean (= duplicate なし、
multi-tool chain 抑制なし)。

### Step 5 — pre/post comparison + cross-condition isolation

別 mutation set (= `/tmp/wave_a_w1_ab.py`) で W1 scenario の routing 効果を
分離:

| config | invoke success | leak rate (= `<ctrl42>`) |
|---|---|---|
| A0 PRE Wave A baseline | 8/10 | 2/10 |
| A1 POST Wave A | 4/10 | 6/10 |
| A2 post-SP + pre-tools | **2/10** | 8/10 (= worst) |
| A3 pre-SP + post-tools | 7/10 | 3/10 (= baseline 近い) |

→ A2 (post-SP + pre-tools) が最悪 = **SP 変更が cause**、 tools= 変更は無害。
cross-condition isolation で「どの軸が effect 持つか」 を pin。

## Methodology Principles

### 数字に踊らされない multi-perspective review

同 scenario でも測定経路で数字違う:
- 実 `reyn chat` subprocess: empty_stop 100% (W5 turn 2)
- programmatic litellm 直接呼び出し A/B: empty_stop 50% (= 同 messages)

→ message envelope の subtle assembly 差で 50pp 振れる。 「数字 1 経路だけで
結論しない」 が funnel の核。 N=10 でも noise window ±10-20pp 大、 N=50+ で
やっと安定推定。

### Mutation 設計の鉄則

1. **1 軸ずつ変える** — V1 で同時 2 変更すると cause 不能
2. **副作用 dimension も記録** — empty_stop だけでなく duplicate tool_call、
   reply 強制、 UX violation 等を分離 measure
3. **baseline は trace の literal replay** — programmatic reconstruction は
   subtle assembly bug を含み得る (= 50pp 乖離の origin)
4. **Confound を見たら追い isolation** — V4 の brevity+order は分離必須

### 介入 layer 優先順位

P3 (OS = runtime engine) + care boundary (= pre-call structural env = OS
responsibility) と整合した layer 優先順位:

| layer | 例 | 介入 cost | 副作用 risk |
|---|---|---|---|
| 1. envelope / protocol | role 遷移、 message 順序、 必須 field | 低 (= 数行 code) | 低 (= protocol-defined surface) |
| 2. schema / tool definition | enum constraint、 parameter schema | 中 | 低 |
| 3. system prompt content | rule wording、 ordering、 inline list | 低 | **高** (= attractor re-shuffle) |

SP 介入は最後の手段。 LLM は SP rule で reasoning するより attractor で
動くケースが多く、 **どんな SP 構造変更でも regression risk が non-trivial**。

## Envelope-fix の実装パタン

### Reyn での fix code (= commit `aab6be2`)

```python
# src/reyn/llm/llm.py:call_llm_tools
if messages and isinstance(messages[-1], dict) and messages[-1].get("role") == "tool":
    messages = messages + [{"role": "user", "content": "(answered)"}]
```

= **2 行**。 SP 一切 touch せず、 history persistence にも影響なし (= LLM call
boundary でのみ inject、 chat history record は inject 前のまま clean)。

### Neutral state signal の選定基準

trailing user message の content は以下の 3 性質を満たす必要:

1. **state 通知 (= imperative ではない)**: 「次 turn で X せよ」 と命じない、
   「前 turn の question は応答済」 を述べる
2. **multi-tool chain freedom 保持**: text reply / next tool call の選択は
   LLM 自由
3. **副作用なし**: 同 tool 再 invoke / reply 強制 / UX 違反を引き起こさない

OK candidates:
- `"(answered)"` — parenthesised neutral state signal、 10 chars
- `"(tool result)"` — observation signal、 13 chars

NG candidates (= 副作用問題):
- `"(continue)"` — 70-100% で同 tool 再 invoke
- `"Reply in text now."` — text 強制 = multi-tool chain blocker
- `"Now confirm."` — 100% で tool 再 invoke
- `"thanks"` — "You're welcome!" reply (= UX 違反)
- empty string `""` — 効果なし (= baseline と同じ)

### Workaround の自覚

envelope-layer fix は **adapter pattern**、 真の fix は provider 側:
- model 改善で attractor 自然消滅した時点で obsolete
- 別 provider (= claude / gpt-5+) 移行で不要になる可能性
- 多言語: 英語 directive `"(answered)"` が reply 言語を英語に bias する可能性、
  「workaround を多言語 engineering しない」 割り切り
- code に **「workaround」 明示 comment** を付ける (= future contributor が
  remove 判断可能に)

## Future application

### このパターンが適用可能な場面

LLM 挙動問題で以下の症状を示す時、 envelope-layer fix を最初に検討:

1. SP を弄っても N≥10 で数字が wide range (= ±30pp 以上) に振れる
2. 「論理的に正しい SP rule」 が無視される (= 30-50% rule violation)
3. tool_use protocol の特定 turn (= post-tool / multi-tool / parallel call) で
   集中的に attractor 発火
4. provider / model 違いで挙動が大きく変わる (= compat layer 由来の quirk
   候補)

### 対象外 (= envelope では解けない)

1. SP 内 instruction の意味的曖昧さ (= "improve" の解釈違い等) → SP rewriting layer
2. tool description の不正確さ → tool definition layer
3. 真の LLM 能力不足 (= reasoning depth 足りない) → 強モデル移行

## References

- giveup-tracker `G12: Pattern E` section: `docs/deep-dives/journal/dogfood/giveup-tracker.md`
- giveup-tracker `G23` section (= Wave A revert evidence): 同上
- code: `src/reyn/llm/llm.py:call_llm_tools` (= envelope inject 実装)
- mutation isolation script: `/tmp/wave_a_min_context.py` (V0-V4) / `/tmp/wave_a_v5_v6.py` (V5-V7) / `/tmp/wave_a_trailing_user.py` (V0-V11) / `/tmp/wave_a_w1_ab.py` (cross-isolation)
- 業界 practice: `docs/deep-dives/journal/insights/2026-05-07-industry-tool-discovery-survey.md` (= 同 session で並行調査)
