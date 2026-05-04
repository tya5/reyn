---
id: B8-S5b
batch: 8
scenario: S5b
date: 2026-05-05
bug_ref: B8-S5b-NEW (input artifact type mismatch: unknown vs eval_builder_request)
status: refuted
verdict: refuted
---

# B8-S5b: eval_builder 単独直接 invoke (構造データ経路)

| Field | Value |
|---|---|
| Date | 2026-05-05 |
| main HEAD | `8e150191db20cb492df511e28492fbf8c866f4d3` |
| Verdict | **refuted** |
| B7 baseline | refuted ([B7-S5b](../../2026-05-04-batch-7-post-infra-verify/findings/B7-S5b-eval-builder-structured.md)) |
| Predicted top | 55% verified (preprocessor anyOf fix `3cbe983` が union input を受理) |

## Setup

- model: `openai/gemini-2.5-flash-lite` via LiteLLM proxy localhost:4000
- input: `eval_builder で direct_llm を analyze して、 target_skill=direct_llm`
- state: fresh (`rm -rf .reyn/`)
- reyn.yaml: `python.trusted: allow` 一時追加 (dogfood 専用、commit 対象外)
- flag: `--allow-untrusted-python`
- trace: `REYN_LLM_TRACE_DUMP=.reyn/llm_trace_b8s5b.jsonl`
- run timestamp: 2026-05-05T07:05:43 – 07:05:50

## Observation

### dogfood_trace --mode summary

```
[Skill Chain]  (1 workflow(s))
  [2026-05-05T07:05:45] eval_builder (entry=analyze_skill)  status=active
    phases: analyze_skill
    run_id: 20260504T220545Z_eval_builder

[Tool Calls]  (1 important tool call(s))
  [ 1] invoke_skill({"name": "eval_builder", "input": {"target_skill": "direct_llm"}})  caller=default

Cost: $0.000458  |  4,089 tokens  |  2 calls
```

### dogfood_trace --mode chain

```
[T+1.0s] tool: invoke_skill({"name": "eval_builder", "input": {"target_skill": "direct_llm"}})
[T+2.0s] workflow_started: eval_builder  run_id=20260504T220545Z_eval_builder
  [T+2.0s] phase_started: analyze_skill
  [T+4.0s] preprocessor_step_failed (step_index=0, type=python)
skill_run_failed: eval_builder
```

### router invoke の詳細 (trace entry 1)

```json
{
  "kind": "response",
  "tool_calls": [{
    "function": {
      "name": "invoke_skill",
      "arguments": "{\"name\": \"eval_builder\", \"input\": {\"target_skill\": \"direct_llm\"}}"
    }
  }],
  "tool_calls_count": 3
}
```

**skill 名**: `eval_builder` ✅ (hallucinate なし)
**input**: `{"target_skill": "direct_llm"}` ← `type` フィールドなし

router が `eval_builder` を正しく選択し、`target_skill=direct_llm` の構造を正確に渡した。
G3 dedupe: 3 invoke attempt のうち 2 件が `duplicate_invoke_skill_in_round` で deduped。

### preprocessor 失敗詳細

```json
{
  "type": "python_step_failed",
  "data": {
    "phase": "analyze_skill",
    "step_index": 0,
    "module": "./analyze_skill_resolver.py",
    "function": "compute_paths",
    "kind": "ValueError",
    "error": "Cannot extract skill name from user_message text: ''. Please use the form \"Generate spec for skill named <name>\" or pass a structured eval_builder_request artifact with target_skill set."
  }
}
```

```json
{
  "type": "skill_run_failed",
  "data": {
    "skill": "eval_builder",
    "error": "Phase 'analyze_skill' preprocessor step[0] python ./analyze_skill_resolver.py:compute_paths: ..."
  }
}
```

### artifact_created (input artifact)

```json
{
  "type": "artifact_created",
  "data": {
    "phase": "_input",
    "artifact_type": "unknown",
    "keys": ["target_skill"],
    "path": ".reyn/artifacts/eval_builder/_input/v01_unknown.json"
  }
}
```

artifact 内容: `{"target_skill": "direct_llm"}`

### detect_attractor

```
Total LLM calls: 2
Detected attractors: 0 (0%)
```

attractor 発生なし。router が 1 turn で invoke_skill を発行。

### eval.md 生成確認

```
.reyn/eval_builder_work/direct_llm/ NOT created
eval.md NOT generated
```

## 根本原因分析: 新 blocker (B7-S5b とは独立)

B7-S5b の失敗原因は `preprocessor_typing.py` の anyOf compile-time bug (fix: `3cbe983`) だった。
B8-S5b では compile は成功し、eval_builder skill は起動・analyze_skill phase に到達した。
`3cbe983` の anyOf fix は有効 ✅。

しかし B8 では別の blocker が顕在化した:

### input artifact の type mismatch

router が `invoke_skill(name="eval_builder", input={"target_skill": "direct_llm"})` を発行した際、
`input` dict には `type` フィールドが含まれていなかった。

OS が artifact を生成する際、`type` フィールドがないため `artifact_type = "unknown"` に分類された。

`analyze_skill_resolver.py:_extract_skill_name` は:
```python
artifact_type = artifact.get("type", "")

if artifact_type == "eval_builder_request":
    # target_skill を直接取得 — ここには到達しない
    ...

# user_message fallback
text = str(data.get("text", "")).strip()  # data に "text" がないので ""
```

`data = {"target_skill": "direct_llm"}` だが `data.get("text", "") = ""`。
regex マッチが失敗し ValueError が発生。

### 原因の連鎖

```
invoke_skill(input={"target_skill": "direct_llm"})
→ OS が artifact type を推定 → type フィールドなし → "unknown"
→ compute_paths(artifact) が呼ばれる
→ artifact.get("type") == "" → eval_builder_request 分岐に入らない
→ data.get("text", "") == "" → regex fallback も失敗
→ ValueError
```

### anyOf fix (`3cbe983`) との関係

`3cbe983` は `preprocessor_typing.py` の compile-time anyOf validation bug を fix した。
この fix により B7-S5b の compile 失敗は解消された — skill が起動できるようになった点は改善。

しかし runtime での artifact type 判定は compiler レベルとは別の問題であり、
`3cbe983` の scope 外。

## Delta vs batch 7

| 項目 | B7-S5b (578bb03) | B8-S5b (8e15019) |
|---|---|---|
| 失敗段階 | DSL コンパイル (起動前) | preprocessor runtime (analyze_skill phase 内) |
| 失敗理由 | preprocessor_typing anyOf compile bug | input artifact type = "unknown" → compute_paths で ValueError |
| LLM calls | 0 (compile 前失敗) | 2 (router 1 + なし) |
| eval_builder 起動 | NO | YES ✅ |
| analyze_skill 到達 | NO | YES ✅ |
| anyOf compile fix 効果 | N/A (これが bug) | ✅ 解消 (compile 成功) |
| preprocessor runtime 到達 | NO | YES ✅ |
| preprocessor 成功 | NO | NO (新 blocker) |
| eval.md 生成 | NO | NO |
| cost | $0.000000 | $0.000458 |

### routing の改善 (B5a との比較)

S5b では router が入力 `eval_builder で direct_llm を analyze して、 target_skill=direct_llm` を
`invoke_skill(name="eval_builder", input={"target_skill": "direct_llm"})` に正確に変換した。
S5a の routing 失敗 (eval skill 誤誘導) とは対照的に、skill 名の明示 (`eval_builder で`) が
router の正確な intent 解釈を助けた。

## Verdict reasoning

- router が `eval_builder` を正しく選択: ✅ hallucinate なし (S5b における router enum fix の効果 confirmed)
- anyOf compile fix (`3cbe983`) が有効: ✅ skill 起動・analyze_skill 到達 (B7 からの改善)
- preprocessor が analyze_skill に到達: ✅
- しかし compute_paths が `input artifact type = "unknown"` で失敗: ❌ 新 blocker
- eval.md は生成されなかった: ❌

verdict: **refuted** — anyOf fix と routing は改善されたが、artifact type mismatch という新 blocker で preprocessor が失敗。

## Fix direction (batch 9)

`_extract_skill_name` に artifact 型による 3 パターン対応を追加:

1. `type == "eval_builder_request"` → `data.target_skill` (現行)
2. `type == ""` または `type == "unknown"` かつ `"target_skill" in data` → `data.target_skill` (新規)
3. user_message fallback → `data.text` から regex 抽出 (現行)

または `invoke_skill` が発行する input に `type: "eval_builder_request"` を含めるよう
eval_builder の routing 例文 / invoke フォームを更新する (skill 側 fix)。

## Implications

- anyOf compile fix (`3cbe983`) は確実に有効: B7 の compile-time blocking は解消
- router enum fix (`9ee6ae1`) の効果: S5b において `eval_builder` 名での invoke は確実 (skill 名明示があれば)
- 新 blocker は runtime の artifact type 判定: compile-time bug からの移行は進歩だが未完
- S5a の routing 失敗と S5b の runtime 失敗は独立した別 issue — 両方 fix が必要
- batch 9 では (1) `_extract_skill_name` の unknown type 対応、(2) S5a の routing ambiguity 対策 の 2 fix が必要
