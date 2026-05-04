---
id: B7-S5a
batch: 7
scenario: S5a
date: 2026-05-04
bug_ref: none (new finding)
status: refuted
verdict: refuted
---

| Field | Value |
|---|---|
| Date | 2026-05-04 |
| main HEAD | `578bb03` |
| Scenario | S5a (chat 経由・自然言語) |
| Verdict | **refuted** |

# B7-S5a: eval_builder 単独直接 invoke (自然言語経路)

## Setup

- model: `openai/gemini-2.5-flash-lite` via LiteLLM proxy localhost:4000
- input: `eval_builder で direct_llm の eval.md を作って`
- state: fresh (`rm -rf .reyn/`)
- reyn.yaml: `python.trusted: allow` 一時追加 (dogfood 専用)
- flag: `--allow-untrusted-python`
- run timestamp: 2026-05-04T18:26:44 – 18:26:54

## Action

```bash
reyn chat default --cui --no-restore --allow-untrusted-python
# input: eval_builder で direct_llm の eval.md を作って
```

## 観測

### dogfood_trace --mode summary

```
[Skill Chain]  (0 workflow(s))

[Tool Calls]  (1 important tool call(s))
  [ 1] invoke_skill({"name": "eval_builder.eval_md", "input": {"agent_name": "direct_llm"}})  caller=default

[Peer Failures / Chain Discards]  (0 event(s))

Cost: $0.000193  |  1,625 tokens  |  1 calls
```

### dogfood_trace --mode chain

```
[T+4.0s] tool: invoke_skill({"name": "eval_builder.eval_md", "input": {"agent_name": "direct_llm"}})
```

### イベントログ抜粋

```jsonl
{"type": "tool_call_deduped", "data": {"name": "invoke_skill", "reason": "duplicate_invoke_skill_in_round"}}
{"type": "tool_call_deduped", "data": {"name": "invoke_skill", "reason": "duplicate_invoke_skill_in_round"}}
{"type": "tool_called", "data": {"tool": "invoke_skill", "args": {"name": "eval_builder.eval_md", "input": {"agent_name": "direct_llm"}}}}
{"type": "tool_failed", "data": {"message": "ValueError: skill 'eval_builder.eval_md' not found; available: ['direct_llm', 'eval', 'eval_builder', ...]"}}
```

### router invoke 経路

- router が `invoke_skill` を呼び出した: **YES**
- skill 名: `eval_builder.eval_md` (**hallucination** — 正しくは `eval_builder`)
- input field: `agent_name` (**hallucination** — 正しくは `target_skill`)
- G3 dedupe: 3 invoke attempt のうち 2 件が `duplicate_invoke_skill_in_round` で deduped
- skill 起動: **失敗** (ValueError: skill not found)

### preprocessor 結果

eval_builder skill が起動できなかったため preprocessor は未実行。

### eval.md 生成確認

```
reyn/local/direct_llm/eval.md NOT created
```

## 6 軸評価

| 軸 | 評価 | 備考 |
|---|---|---|
| 応答品質 | NG | hallucinated skill 名・field 名、skill 起動失敗 |
| 意図解釈 | NG | `eval_builder` を `eval_builder.eval_md` と解釈 |
| 待ち時間 | OK | 4s で完了 (ただし失敗のため) |
| 見せ方 | NEUTRAL | error message がそのまま表示 |
| エラー UX | NG | user には "Please try a different approach" のみ |
| state 整合性 | OK | WAL に 2 entry (inbox_put / inbox_consume)、skill_run なし |

## prediction 評価

事前 prediction (分布形式):
- internal metric (5a): **55% verified / 30% inconclusive / 15% refuted**

実際の verdict: **refuted**

top probability category は verified (55%) だったが、実際は refuted (15%)。
**prediction MISS** (top probability が外れ)。

### MISS の理由

router LLM が `eval_builder` skill を正しく認識できず、`eval_builder.eval_md` という
存在しない dotted-name で invoke を試みた。さらに input field も `target_skill` ではなく
`agent_name` を hallucinate。

skill_improver 経由 (S1-S4) では router が `invoke_skill(name="eval_builder")` を
正しく呼ぶため、この hallucination は `eval_builder` の `when_to_use` / `examples`
の記述と直接 invoke 経路の間に discrepancy があることを示す可能性がある。

あるいは weak LLM (gemini-2.5-flash-lite) が eval_builder の routing 記述を
`eval_builder.eval_md` のような形式で解釈することが問題の本質かもしれない。

### G3 dedupe 観測

G3 (duplicate_invoke_skill_in_round) が 2 件 fire。これは S2 (B5-M1) の
dedupe 効果を S5a でも確認できたことを意味する — ただし対象 skill が存在しないため
cost 削減効果の直接観測は困難。

## verdict 根拠

- eval_builder skill が起動できなかった: router の skill 名 hallucination
- `eval_builder.eval_md` という存在しない skill 名を LLM が生成
- input field も hallucinate (`agent_name` vs 正しい `target_skill`)
- eval.md は生成されなかった
- union input (user_message 経路) の preprocessor は未到達

verdict: **refuted** — 5a 経路では eval_builder が invoke されなかった。
これは eval_builder fix (`e6de782`) の独立効果を verify できなかったことを意味する。

## next action

- eval_builder の `when_to_use` / `examples` を router LLM が正しく認識できるよう
  skill.md の routing セクションを改善する
- 特に skill 名の dotted-name 形式 hallucination を防ぐために、examples の
  negative 例を強化する
- あるいは `reyn run eval_builder "..."` (CLI 直接) で S5b の観測に切替
