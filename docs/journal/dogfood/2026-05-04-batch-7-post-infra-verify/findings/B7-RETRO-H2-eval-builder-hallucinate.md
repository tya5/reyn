# B7-RETRO-H2: eval_builder input field hallucination — retroactive verification

| Field | Value |
|---|---|
| Date | 2026-05-04 |
| main HEAD | 269bdb6 |
| Original hypothesis | router が `eval_builder.eval_md` という存在しない skill 名を hallucinate し、 かつ input field も `agent_name` (正しくは `target_skill`) を hallucinate |
| Original verdict | refuted (B7-S5a-eval-builder-natural.md) — eval_builder 起動失敗 |
| **NEW verdict** | **partially verified (observation-based)** — 今回の run では skill 名は正しく `eval_builder` を生成したが、 input field は引き続き hallucinate。 `invoke_skill.name` enum 制約なし問題は H1 と同根、 input field 問題は別原因 |
| Trace file | `.reyn/llm_trace_h2.jsonl` |
| Main request_id | `b8baf34e-8f19-443c-9d94-ffbdbd25bbe7` |

---

## Setup

```bash
rm -rf .reyn/
REYN_LLM_TRACE_DUMP=.reyn/llm_trace_h2.jsonl \
  reyn chat default --cui --no-restore --allow-untrusted-python
# input: eval_builder で direct_llm の eval.md を作って
# /quit
```

## Action

```bash
python scripts/dogfood_trace.py --mode llm-payloads --trace .reyn/llm_trace_h2.jsonl
python scripts/dogfood_trace.py --mode llm-detail b8baf34e-8f19-443c-9d94-ffbdbd25bbe7 --trace .reyn/llm_trace_h2.jsonl --full
python scripts/dogfood_trace.py --mode llm-detail 619cc8a8-5a40-4167-98df-48a95402005b --trace .reyn/llm_trace_h2.jsonl --full
```

## 実 payload 観測

### llm-payloads 出力

```
[T+0.0s] request_id=b8baf34e-...  caller=router  msgs=4  tools=11  finish=tool_calls  tokens_in=1523
[T+1.1s] request_id=619cc8a8-...  caller=router  msgs=6  tools=11  finish=stop  tokens_in=1654
```

### 第 1 router call — LLM response

```
tool_calls (1):
  - invoke_skill  args={"name": "eval_builder", "input": {"output_file": "eval.md", "skill_name": "direct_llm"}}
```

スキル名 `eval_builder` は正しい。しかし input fields が hallucination:
- `output_file`: 存在しない field
- `skill_name`: 存在しない field (正しくは `target_skill`)

### tool return (eval_builder 実行試行の結果)

```json
{"status": "ok", "data": {"status": "error", "data": {"error": "failed to load eval_builder: Phase 'analyze_skill': preprocessor step[0] (type='python'): 'into' parent path 'data' not found in schema."}}}
```

eval_builder が失敗した理由は input field 不一致ではなく、 eval_builder 自身の
preprocessor 内部バグ (`'into' parent path 'data' not found in schema`)。

### 第 2 router call — LLM response

```
finish_reason: stop
content: The `eval_builder` skill seems to have encountered an error because the `output_file`
and `skill_name` parameters were not found in its schema. Could you please provide the
correct parameters for the `eval_builder` skill?
```

LLM は error message を受け取り、 自分が渡した fields が問題だったと理解しているが、
eval_builder 自身のバグ (preprocessor 内部エラー) との混同が起きている。

### invoke_skill.name の enum 制約確認

H1 と同一 router 実装のため、 `invoke_skill.name` には enum 制約なし (confirmed H1 から)。

### eval_builder ツール schema の有無

tools schema に `eval_builder` 自体の description は存在しない (invoke_skill 経由で呼ぶため)。
LLM は `eval_builder` の input schema (= `target_skill` field が正しい) を知る手段がない。
`describe_skill` を呼ばずに直接 `invoke_skill` したため、 input schema を参照せずに
`output_file` / `skill_name` という hallucinated fields を生成した。

## 旧推測との比較

| 項目 | 旧推測 | 観測結果 |
|---|---|---|
| skill 名 hallucination | `eval_builder.eval_md` が hallucinate された | **部分的: 今回は正しく `eval_builder` を生成**。 B7-S5a (旧観測) の `eval_builder.eval_md` は確率的挙動の別 variant |
| input field hallucination | `agent_name` が生成された | **引き続き発生**: 今回は `output_file` / `skill_name` が生成された。 field 名は run ごとに異なるが「正しくない」点は共通 |
| 原因 (skill 名) | B7-NEW-1 と同じ enum 不在問題か? | **同根**: enum 制約なし + system prompt に flat list なし → ゼロショット生成 |
| 原因 (input field) | describe_skill 経由でないと見えない | **観測で確定**: LLM は `describe_skill` を呼ばずに `invoke_skill` を直接呼び、 input schema を参照しなかった |
| eval_builder 失敗の原因 | input field 不一致 | **eval_builder 自身の preprocessor bug が別途存在**: `'into' parent path 'data' not found` — これは B7-S5b で観測されている別問題 |

## 真因 (observation-based)

**2 層の問題が重なっている**:

1. **`invoke_skill` input field hallucination**: `describe_skill` を呼ばずに直接 `invoke_skill` した
   場合、 LLM は eval_builder の正しい input schema (`target_skill`) を知る機会がない。
   ゼロショットで input fields を推測するため `output_file` / `skill_name` / `agent_name` (旧観測) などが生成される。
   → fix: `describe_skill` 呼び出しを stronger に promote するか、 または `invoke_skill` schema に
   「input artifact の構造は describe_skill で確認せよ」を明示。

2. **eval_builder preprocessor bug (独立問題)**: `analyze_skill` フェーズの preprocessor が
   `'into' parent path 'data' not found in schema` で失敗している。
   これは input field 不一致とは独立した eval_builder 内部の bug (= B7-S5b で別途追跡)。

## B7-NEW-1 との同因確認

| 問題 | H1 (B7-NEW-1) | H2 (B7-S5a) |
|---|---|---|
| `invoke_skill.name` enum なし | YES | YES (同根) |
| system prompt に flat list なし | YES | YES (同根) |
| `describe_skill` 未呼び出し | YES | YES (同根) |
| input field hallucination | YES (`skill`, `times` 等) | YES (`output_file`, `skill_name`) |
| skill 名 dot-notation hallucination | YES (1st run) | NO (今回 run では正しい) |

**結論: H1 と H2 は `invoke_skill.name` enum なし + flat skill list 不在という共通原因を持つ**。
さらに H2 には describe_skill スキップによる input field hallucination の追加問題がある。
1 fix (enum 動的 injection + flat list inject) で H1 も H2 の skill 名問題も解消する可能性が高い。
H2 の input field 問題には describe_skill の強制促進が別途必要。

## next action

- H1 と同じ fix direction (enum injection + flat skill list) を 1 PR に統合
- `describe_skill` の呼び出しを promote する system prompt ルールの有効性を評価
  (現在「call describe_skill first if unsure」という soft instruction あり — 強化を検討)
- eval_builder preprocessor bug は別 PR (B7-S5b) で追跡
