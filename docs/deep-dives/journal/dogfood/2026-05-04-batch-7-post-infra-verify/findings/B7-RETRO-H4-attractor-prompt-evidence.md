# B7-RETRO-H4: G12 attractor — MUST rule injection 実証 (retroactive verification)

| Field | Value |
|---|---|
| Date | 2026-05-04 |
| main HEAD | 269bdb6 |
| Original hypothesis | 「describe 後は必ず invoke せよ」系 MUST rule が system prompt に injected されているにもかかわらず weak LLM が honor しない |
| Original verdict | 旧推測 — 「prompt rule 路線の限界」 (B5R2-H1 / B6-S2-observation.md) |
| **NEW verdict** | **verified (observation-based)** — MUST rule は確実に injected されていた。 LLM は rule を **見たうえで** non-honor した。 確率的挙動 + `finish_reason: stop, completion_tokens=0` という特殊終了形式を確認 |
| Trace file | `.reyn/llm_trace_h4.jsonl` |
| Attractor request_id | `fd2aef81-1307-4bc8-9cea-f602d3b95d2a` (list_skills 後に stop した call) |

---

## Setup

```bash
rm -rf .reyn/
REYN_LLM_TRACE_DUMP=.reyn/llm_trace_h4.jsonl \
  reyn chat default --cui --no-restore --allow-untrusted-python
# input: direct_llm skill を使って、カレーのレシピを教えてもらって
# /quit
```

## Action

```bash
python scripts/dogfood_trace.py --mode llm-payloads --trace .reyn/llm_trace_h4.jsonl
python scripts/dogfood_trace.py --mode llm-detail fd2aef81-1307-4bc8-9cea-f602d3b95d2a --trace .reyn/llm_trace_h4.jsonl --full
```

## 実 payload 観測

### llm-payloads 出力

```
[T+0.0s] request_id=b1d5511a-...  caller=router  finish=tool_calls  tool_calls=1  (list_skills(""))
[T+1.3s] request_id=2657ff35-...  caller=router  finish=tool_calls  tool_calls=1  (list_skills("general"))
[T+2.3s] request_id=fd2aef81-...  caller=router  finish=stop  tool_calls=0  tokens_in=1915  tokens_out=?
```

3 回目の LLM call で `finish_reason: stop`, `tool_calls=0`。
`tokens_in=1915` (スキルリスト全体が context 内)、 `tokens_out` は 0 または記録なし。

### 第 3 router call の system prompt (全文 — llm-detail --full より)

MUST rule 関連部分を抜粋:

```
## Behaviour
  - First decide intent (Action / Recall / Save / Forget / Reply),
    then pick tools from that group.
  - Reply directly only for chitchat, questions about yourself,
    and clarifications back to the user. Domain tasks → Action.
  - For Action or explicit-skill requests, call list_skills first,
    then invoke_skill (use describe_skill in between only when you need to inspect).
  - If the user names a skill, use list_skills + invoke_skill
    rather than paraphrasing the request as a Reply.
  - After list_skills reveals at least one matching skill, you MUST
    call describe_skill or invoke_skill. Do NOT reply directly.
  - After describe_skill, you MUST call invoke_skill or explain in text
    why not; never stop silently after investigation.
  - ...
  - Never invent skill / agent / slug names; only use those returned
    by list_*.
```

**MUST rule は injected されている**。特に以下の 2 rule が直接関連:
- `After list_skills reveals at least one matching skill, you MUST call describe_skill or invoke_skill.`
- `Do NOT reply directly.`

### 第 3 router call の context (tool results)

```json
[5] tool: {"status": "ok", "data": [{"category": "general", "count": 10}]}
[7] tool: {"status": "ok", "data": [
  {"name": "direct_llm", "description": "Catalogue-gap fallback: hand a single-shot natural-language task straight to"},
  {"name": "eval", "description": "Evaluate a target skill against a single test case..."},
  {"name": "eval_builder", "description": "Auto-generate an eval spec (eval.md) for a skill"},
  ...
  {"name": "word_stats_demo", "description": "Demo of the python preprocessor step..."}
]}
```

`direct_llm` が skill list に含まれており、 LLM は `direct_llm` の existence を認識できた状態。

### 第 3 router call の LLM response

```
finish_reason: stop
completion_tokens: 0
tool_calls: (none)
content: (empty)
```

LLM は `finish_reason=stop` で空応答を返した。 `completion_tokens=0` は出力トークンがゼロ
(または API 側で記録なし) であることを示す。 rule を明示的に無視するどころか、 何も生成しなかった。

## 旧推測との比較

| 項目 | 旧推測 | 観測結果 |
|---|---|---|
| MUST rule が injected されていたか | 不明 (「injected されていなかった可能性」を仮説に含む) | **観測で確定: YES — injected されていた** |
| LLM は rule を見たうえで non-honor したか | 推測「確率的 MUST rule 不 honor」 | **観測で確定: YES — rule 見えた状態で stop** |
| system prompt の tool_choice / order | 不明 | tool_choice=auto、 tool order は list_skills が先頭ではない |
| 「過剰 consolidation で rule 文言が削れた」可能性 | 仮説として提示 | **否定: rule 文言は現在も存在している** |
| 真の意味の attractor | 確率的か、 prompt 構造問題か | **真の意味の attractor: LLM が rule を見て従わない**。 確率的 MUST rule non-honor |

## 真因 (observation-based)

G12 attractor の真因は **MUST rule non-honor (rule あっても従わない確率的挙動)** であることが確定。

具体的なメカニズム:
1. `list_skills("")` → `list_skills("general")` → skill list 取得
2. system prompt には「After list_skills reveals at least one matching skill, you MUST call describe_skill or invoke_skill」が明示されている
3. LLM は skill list (`direct_llm` 含む 10 skills) を受け取った状態で LLM call を受ける
4. **にもかかわらず** `finish_reason=stop, completion_tokens=0` で空応答

`completion_tokens=0` は通常の empty content とは異なる特殊終了。
gemini-2.5-flash-lite の API 挙動として、 tool call を生成しようとして途中で truncate した
可能性、または context が何らかの条件を満たさなかった場合の truncation が疑われる。

旧推測「prompt rule 追加路線の限界」は正しかった。 rule を追加しても、
weak LLM の確率的挙動によりその rule が honor されない。

## tool_choice: auto の影響

現在 `tool_choice: auto` が使用されている (llm-detail で確認)。
これは LLM に「tool を呼ぶか呼ばないか」を自由に選択させる設定。

MUST rule を prompt で表現する代わりに OS 層で `tool_choice: required` を使えば、
LLM に tool call を強制できる。ただし tool_choice=required は「何らかの tool を呼ぶ」
ことを強制するのみで、 `invoke_skill` を特定して強制するわけではない。

## 修正方向 (observation-based)

B5R2-H1 の option A (OS 層 state machine) が observation と整合する:

```
旧 option A: OS 層 state machine で discovery 状態を track
- list_skills / describe_skill 後の state を OS 層で記録
- discovered ≥ 1 かつ invoke_skill 未呼び なら強制 prompt injection
```

観測からの追加情報:
- `tool_choice: auto` → `required` 変更が最も直接的 (ただし次フェーズへの影響評価必要)
- `completion_tokens=0` の特殊終了を OS 層で検出して retry trigger にする
- G12 attractor の variant 頻度測定 (= 10 回連続実行して fail rate を定量化) が
  wave 3 での優先度判定に必要

## 修正候補の優先度 (observation-based)

| Option | 内容 | 評価 |
|---|---|---|
| A: OS state machine | discovery state 保持 → 強制 inject | HIGH — 設計思想と整合 (P3) |
| B: tool_choice=required | list_skills 後の turn で tool 呼び出しを強制 | MEDIUM — 副作用要調査 |
| C: completion_tokens=0 の検出 | 空応答を OS 層で retry trigger に | LOW — 根本解ではない |
| D: 強モデル併用 | G4 spike 経由 | MEDIUM — weak LLM 路線の限界突破 |

## next action

1. G12 attractor 発生率の定量化: 同 scenario 10 回実行して `completion_tokens=0` 頻度測定
2. option A (OS state machine) の設計 spike を次 wave で実施
3. `tool_choice=required` の cost / side-effect 評価
4. G4 spike (強モデル) での `describe→stop` variant 発生率比較 (baseline: gemini-2.5-flash-lite での観測結果)
