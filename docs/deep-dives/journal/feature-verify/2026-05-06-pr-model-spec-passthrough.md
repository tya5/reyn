# PR-MODEL-SPEC e2e passthrough verify

| Field | Value |
|---|---|
| Date | 2026-05-06 |
| main HEAD | `e008a70` |
| Verdict | **verified** ✅ |
| Classification | 🟡 仕様変更 (= additive backward compat) |

## What was verified

PR-MODEL-SPEC (`e008a70`) で導入した `reyn.yaml` `models:` の **dict form passthrough**
が real LLM dogfood で **operator-declared kwargs を litellm に届ける** ことを
end-to-end で確認。

## Setup (= reyn.local.yaml temporary、 verify 後に restore)

```yaml
models:
  light:    openai/gemini-2.5-flash-lite              # str form (= backward compat)
  standard:                                           # dict form (= verify target)
    model: openai/gemini-2.5-flash-lite
    temperature: 0.3
    extra_body:
      reyn_dogfood_marker: pr-model-spec-verify       # gemini が ignore する custom marker
  strong:   openai/gemini-2.5-flash-lite
```

## Action

1 dogfood session: `skill_improver で direct_llm を 1 回 review して改善案を出して`
(= batch 14 と同 input)。 `REYN_LLM_TRACE_DUMP` で全 LLM call payload を JSONL dump。

## Observation

### `spec_kwargs` field (= dump record の operator-declared kwargs)

```
Total request frames: 47
Frames with non-empty spec_kwargs: 40
Distinct spec_kwargs combinations: 1
  {"temperature": 0.3, "extra_body": {"reyn_dogfood_marker": "pr-model-spec-verify"}}
```

= **40 phase LLM call 全てで operator declared kwargs が consistent に carry**。
callers = `phase:prepare` / `phase:analyze_skill` / `phase:write_eval` / 等の phase LLM
calls。

### Chain completion

```
skill_improver (entry=prepare) status=finished
  phases: prepare → run_and_eval → plan_improvements → apply_improvements → finalize
```

= chain 完走、 narrator まで到達、 cost $0.0246 (= dict form でも cost characteristics
変化なし、 単に kwargs を litellm に渡しているだけ)。

### Backward compat (= str form `light` / `strong`)

`light` / `strong` は str form のまま、 `ModelSpec(model=..., kwargs={})` で resolve、
spec_kwargs={} で litellm 呼び出し。 既存挙動完全維持 ✅。

## Verdict reasoning

- **dict form parsing**: ✅ ModelResolver が dict form を正しく ModelSpec 化
- **passthrough end-to-end**: ✅ phase 40 call 全てで kwargs carry、 litellm に届く
- **backward compat**: ✅ str form も同じ resolve path で kwargs={} となり問題なし
- **chain 完走**: ✅ 仕様変更が既存 skill chain 動作に影響なし
- **B14-R1 integration**: ✅ literal hallucinate (= `gpt-4o-mini` が 2 回観察) も
  fallback 正常動作、 dict form と coexist

## Conclusion

PR-MODEL-SPEC は **e2e で operator pre-config kwargs を litellm に届ける機構** として
動作確認。 G4 spike trial (= reasoning model with `extra_body.thinking`) の前提条件
整備完了、 任意 timing で着手可能。
