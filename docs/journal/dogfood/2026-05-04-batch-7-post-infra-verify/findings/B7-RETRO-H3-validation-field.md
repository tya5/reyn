# B7-RETRO-H3: copy_to_work `_validation` → `validation` rename 効果 — retroactive verification

| Field | Value |
|---|---|
| Date | 2026-05-04 |
| main HEAD | 269bdb6 |
| Original hypothesis (a) | `_validation` (underscore prefix) を LLM が internal field と解釈してスキップした。rename により `validation` (no prefix) にすることで LLM が通常 field として読む |
| Original verdict | verified (間接的) — Tier 3 LLMReplay hand-crafted fixture のみ (B6-S1-M1-hypothesis-a-tier3-verify.md) |
| **NEW verdict** | **prerequisite blocked** — `copy_to_work` フェーズに到達できず。 `prepare` フェーズが `eval_builder` preprocessor bug (B7-S5b) によりブロックされるため、 実 LLM payload での `data.validation` 観測は不可能 |
| Trace file | `.reyn/llm_trace_h3.jsonl` (prepare フェーズ trace のみ) |
| Main request_id | `9c465b57-a17b-449b-8972-15554b352d5d` (phase:prepare) |

---

## Setup

```bash
rm -rf .reyn/
REYN_LLM_TRACE_DUMP=.reyn/llm_trace_h3.jsonl \
  reyn chat default --cui --no-restore --allow-untrusted-python
# input: skill_improver を起動して、対象は direct_llm skill を 1 回改善して
# /quit
```

## Action

```bash
python scripts/dogfood_trace.py --mode llm-payloads --trace .reyn/llm_trace_h3.jsonl
python scripts/dogfood_trace.py --mode llm-detail 9c465b57-... --trace .reyn/llm_trace_h3.jsonl
```

## 実 payload 観測

### llm-payloads 出力 (抜粋)

```
[T+0.0s] request_id=ac750f76-...  caller=router  msgs=4  tools=11
         response: finish=tool_calls  tool_calls=3  (invoke_skill(name="skill_improver", ...))
[T+1.9s] request_id=9c465b57-...  caller=phase:prepare  msgs=2  tokens_in=6034
[T+2.0s] request_id=7403183d-...  caller=phase:prepare  msgs=2  tokens_in=6035
[T+3.9s] request_id=e6972e11-...  caller=phase:prepare  msgs=2  tokens_in=6116
[T+7.6s] request_id=dee76863-...  caller=phase:prepare  msgs=2  tokens_in=6196
[T+8.9s] request_id=042ce1e8-...  caller=phase:prepare  msgs=2  (no response record)
```

chain は `phase:prepare` まで到達。 しかし `phase:copy_to_work` は一切観測されなかった。

### router invoke (router request_id=ac750f76-...)

```
tool_calls (3):
  - invoke_skill  args={"name": "skill_improver", "input": {"skill": "direct_llm", "times": 1}}
  - invoke_skill  args={"name": "skill_improver", "input": {"skill": "direct_llm", "times": 1}}
  - invoke_skill  args={"name": "skill_improver", "input": {"times": 1, "skill": "direct_llm"}}
```

今回はスキル名 `skill_improver` が正しく生成された (G3 dedupe が発火し 1 実行に集約)。

### prepare フェーズでのブロック (request_id=e6972e11-...)

prepare フェーズの context に以下の tool return が含まれていた:

```
"'into' parent path 'data' not found in schema. Ensure the parent field is declared
in the input artifact schema or produced by an earlier preprocessor step."
```

prepare フェーズが `eval_builder` を sub_skill として呼び出し (direct_llm の eval.md 生成)、
その eval_builder 自体が preprocessor bug でクラッシュしている。

### prepare → copy_to_work 遷移の未達

prepare フェーズが max_act_turns (= 9) を超えて abort したと推定される。
`copy_to_work` への transition は一切観測されなかった。

## 旧推測との比較

| 項目 | 旧推測 | 観測結果 |
|---|---|---|
| `data.validation` が LLM に届くか | Tier 3 fixture で間接確認 (hand-crafted) | **実 LLM call では未観測** — `copy_to_work` 到達不可 |
| `_validation` prefix の効果 | Python convention で internal と解釈される可能性 | **観測不可** — H3 は prerequisite blocked |
| rename fix (G2, `3cf7412`) の有効性 | Tier 3 replay test で pin 済み | **実 LLM での確認は未達** — Tier 3 は hand-crafted fixture 使用 |
| blocking factor | B7-NEW-1 (router dot-notation) を仮定 | **実際の blocking factor は eval_builder preprocessor bug (B7-S5b)**。 router は正しくスキル名を生成できた |

## 真因 (observation-based)

H3 が観測不可の理由は **B7-S5b: eval_builder analyze_skill preprocessor bug** である:

```
Phase 'analyze_skill': preprocessor step[0] (type='python'):
'into' parent path 'data' not found in schema.
```

この bug が `prepare` フェーズ内の `run_skill(eval_builder)` sub_skill 呼び出しをブロックし、
`prepare` → `copy_to_work` 遷移が一切実行されない。

H1 (dot-notation) は今回の run では発生していない — skill 名生成は確率的で今回は正しかった。
真の blocking factor は B7-S5b であることが observation で確定。

## 元仮説 (a) の現状ステータス

- Tier 3 LLMReplay (hand-crafted fixture) では `data.validation` (no underscore) で
  LLM が正しく判断することを pin 済み
- `_validation` vs `validation` の LLM 挙動差は **実 LLM call では未観測**
- 仮説 (a) の rename fix (`3cf7412`) が実際に effective かどうかを実 LLM で確認するには
  B7-S5b (eval_builder preprocessor bug) を先に fix する必要がある

## 修正方向

H3 自体には「fix すべき新問題」は観測されなかった。

1. **B7-S5b を先に修正**: eval_builder preprocessor bug を fix すれば prepare が完走し、
   `copy_to_work` まで到達できるようになる
2. **fix 後に H3 再観測**: B7-S5b fix 後に同 scenario を再実行し、 `copy_to_work` フェーズの
   LLM call が `data.validation` を正しく受け取っているか実 payload で確認
3. **Tier 3 test は regression guard として維持**: 実 LLM 観測が完了するまでの interim coverage

## next action

1. B7-S5b (eval_builder preprocessor bug) の fix を優先 (prerequisite)
2. fix 後に H3 re-observation session を設定
3. observation 経路: `reyn run skill_improver 'direct_llm を改善して'` または chat 経由で
   `copy_to_work` まで到達させ、 `phase:copy_to_work` の request_id で `llm-detail --full` を実行
