---
id: B8-S5a
batch: 8
scenario: S5a
date: 2026-05-05
bug_ref: B8-S5a-NEW (router mismatch: eval vs eval_builder)
status: refuted
verdict: refuted
---

# B8-S5a: eval_builder 単独直接 invoke (自然言語経路)

| Field | Value |
|---|---|
| Date | 2026-05-05 |
| main HEAD | `8e150191db20cb492df511e28492fbf8c866f4d3` |
| Verdict | **refuted** |
| B7 baseline | refuted ([B7-S5a](../../2026-05-04-batch-7-post-infra-verify/findings/B7-S5a-eval-builder-natural.md)) |
| Predicted top | 65% verified (router enum fix eliminates hallucination, eval_builder invoked correctly) |

## Setup

- model: `openai/gemini-2.5-flash-lite` via LiteLLM proxy localhost:4000
- input: `direct_llm の eval を作って`
- state: fresh (`rm -rf .reyn/`)
- reyn.yaml: `python.trusted: allow` 一時追加 (dogfood 専用、commit 対象外)
- flag: `--allow-untrusted-python`
- trace: `REYN_LLM_TRACE_DUMP=.reyn/llm_trace_b8s5a.jsonl`
- run timestamp: 2026-05-05T07:03:36 – 07:03:59

## Observation

### dogfood_trace --mode summary

```
[Skill Chain]  (6 workflow(s))
  [2026-05-05T07:03:41] eval (entry=run_target)  status=finished
    phases: run_target -> evaluate
    run_id: 20260504T220341Z_eval
  [2026-05-05T07:03:46] direct_llm (entry=respond)  status=finished
  [2026-05-05T07:03:50] judge_phase (entry=judge)  status=finished
  [2026-05-05T07:03:57] skill_narrator (entry=narrate)  status=finished

[Tool Calls]  (7 important tool call(s))
  [ 1] list_skills({"path": ""})  caller=default
  [ 2] list_skills({"path": "general"})  caller=default
  [ 3] describe_skill({"name": "eval"})  caller=default
  [ 4] invoke_skill({"input": {"spec_path": "eval.md", ...}, "name": "eval"})  caller=default

Cost: $0.001246  |  32,287 tokens  |  9 calls (12 total via trace)
```

### dogfood_trace --mode chain

```
[T+2.0s] tool: list_skills({"path": ""})
[T+3.0s] tool: list_skills({"path": "general"})
[T+4.0s] tool: describe_skill({"name": "eval"})           ← eval_builder でなく eval を記述
[T+5.0s] tool: invoke_skill({"name": "eval", ...})        ← eval_builder でなく eval を起動
[T+5.0s] workflow_started: eval  run_id=20260504T220341Z_eval
  [T+5.0s] phase_started: run_target
  [T+10.0s] tool: run_skill({"skill": "direct_llm", ...})
  [T+21.0s] phase_completed: evaluate
[T+21.0s] workflow_started: skill_narrator
  [T+23.0s] phase_completed: narrate
[T+22.5s] router_empty_response_detected: finish=stop, completion_tokens=0
```

### router invoke の詳細 (trace entry 7)

```json
{
  "kind": "response",
  "tool_calls": [{
    "function": {
      "name": "invoke_skill",
      "arguments": "{\"input\": {\"spec_path\": \"eval.md\", \"phase_criteria\": \"\", \"dsl_root\": \"\", \"case_input\": \"direct_llm の eval を作って\", \"case_name\": \"eval_direct_llm_test\", \"target_skill_path\": \"direct_llm\"}, \"name\": \"eval\"}"
    }
  }]
}
```

**skill 名**: `eval` (hallucination — 正しくは `eval_builder`)
**input fields**: `spec_path`, `phase_criteria`, `dsl_root`, `case_input`, `case_name`, `target_skill_path` (全て hallucinate — `eval` skill の実スキーマに存在しない)

### detect_attractor

```
Total LLM calls: 12
Detected attractors: 1 (8%)
  [T+22.5s router] stop_with_must_rule
    MUST rule: "After list_skills reveals at least one matching skill, you MUST"
    finish=stop, completion_tokens=0
```

router_empty_response_detected が 1 件 emit (G12 attractor の残存)。

### eval.md 生成確認

```
.reyn/eval_builder_work/direct_llm/ NOT created
eval.md NOT generated
```

`eval` skill は run/evaluate の結果を生成したが、eval_builder が求める eval.md は未生成。

## Delta vs batch 7

| 項目 | B7-S5a (578bb03) | B8-S5a (8e15019) |
|---|---|---|
| skill 名 hallucinate | `eval_builder.eval_md` (dot-notation) | `eval` (別 skill への誤誘導) |
| input field hallucinate | `agent_name` | `spec_path`, `phase_criteria`, etc. (eval skill input 形式) |
| 失敗パターン | skill not found (ValueError) | eval skill が起動・完走 (wrong skill) |
| eval_builder 起動 | NO | NO |
| eval.md 生成 | NO | NO |
| LLM calls (router) | 1 | 4 |
| cost | $0.000193 | $0.001246 |
| G3 dedupe | 2件 deduped | 0件 deduped |
| attractor | なし | 1件 (stop_with_must_rule) |

### hallucination パターンの変化

B7 では `eval_builder.eval_md` という存在しない dotted-name で失敗した。
router enum fix (`9ee6ae1`) の効果で dot-notation は消失した — これ自体は改善。

しかし B8 では別の hallucination が発生した: router が入力「eval を作って」を
`eval` skill (既存、run/evaluate 担当) への routing と解釈し、eval skill を実際に起動・完走させた。
`eval` skill は `direct_llm` を sub-skill として実行し (evaluate_direct_llm_test 形式)、
judge_phase → narrator まで完走したが、これはユーザーの意図と完全に異なる動作。

B7 の hallucination は「存在しない skill 名」でガードされ即失敗。
B8 の hallucination は「既存 skill への誤誘導」で表面上完走するため、よりタチが悪い。

### router が eval を選んだ理由 (推定)

1. list_skills → list_skills → describe_skill(eval) の順で、eval_builder を describe せず eval を先に describe した
2. `eval_builder` の `when_to_use` にある「eval を作って」という日本語が `eval` skill の keyword と競合
3. weak LLM (gemini-2.5-flash-lite) が「eval を作って」の「作って」(create) より「eval」(run) に引っ張られた
4. enum fix は `skill_improver` 経由の invoke では有効だが、直接 invoke wording では依然として intent misrouting が発生

## Verdict reasoning

- `eval_builder` skill が一切起動しなかった: router が `eval` skill に誤誘導
- B7 の dot-notation hallucination (`eval_builder.eval_md`) は消失 ← router enum fix (`9ee6ae1`) は部分的に有効
- しかし「eval を作って」→ `eval` skill への routing という別の hallucination が発生
- eval.md は生成されなかった
- S5a の一次目的「router が eval_builder を hallucinate なく invoke する」は達成されなかった

verdict: **refuted** — router が eval_builder を invoke せず eval skill を誤起動。
hallucination の形態は B7 から変化したが、routing 誤りは継続している。

## Implications

- router enum fix (`9ee6ae1`) は dot-notation hallucination を排除したが、intent misrouting (eval vs eval_builder) は解消していない
- `eval_builder` の `when_to_use` wording が `eval` skill と競合している可能性がある
- input wording に「eval_builder で」(skill 名を明示) を含む S5b 形式であれば routing は正確 (S5b 観測参照)
- batch 9 fix 方向: eval_builder の `when_to_use.negative` に「eval を『実行』する intent は eval skill を使う」を追加、または例文に「eval_builder で」prefix を追加する
- G12 attractor (stop_with_must_rule) が残存しており、router の 2nd turn で empty stop が発生した — G12 truncation fix の効果は partial
