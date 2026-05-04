---
id: B7-S5b
batch: 7
scenario: S5b
date: 2026-05-04
bug_ref: NEW-B7-S5b (preprocessor_typing anyOf union schema bug)
status: refuted
verdict: refuted
---

| Field | Value |
|---|---|
| Date | 2026-05-04 |
| main HEAD | `578bb03` |
| Scenario | S5b (CLI run 経由・構造データ) |
| Verdict | **refuted** |

# B7-S5b: eval_builder 単独直接 invoke (構造データ経路)

## Setup

- model: `openai/gemini-2.5-flash-lite` via LiteLLM proxy localhost:4000
- input: `'{"type":"eval_builder_request","data":{"target_skill":"direct_llm"}}'`
- state: fresh (`rm -rf .reyn/`)
- reyn.yaml: `python.trusted: allow` 一時追加 (dogfood 専用)
- flag: `--allow-untrusted-python`
- run timestamp: 2026-05-04T18:31 (approx)

## Action

```bash
reyn run eval_builder '{"type":"eval_builder_request","data":{"target_skill":"direct_llm"}}' --allow-untrusted-python
```

## 観測

### エラー出力 (全文)

```
Error: failed to compile DSL '/Users/yasudatetsuya/Workspace/junk/claude_sandbox/sandbox_2/src/reyn/stdlib/skills/eval_builder/skill.md':
Phase 'analyze_skill': preprocessor step[0] (type='python'):
  'into' parent path 'data' not found in schema.
  Ensure the parent field is declared in the input artifact schema or
  produced by an earlier preprocessor step.
resolved: .../src/reyn/stdlib/skills/eval_builder/skill.md  (dsl-root: .../src/reyn/stdlib)
exit code: 1
```

### .reyn 状態

コンパイルが失敗したため `.reyn/` は**作成されなかった**。
WAL なし、events なし、LLM calls なし。

### dogfood_trace 結果

コンパイル前に exit するため `.reyn/` が存在せず、dogfood_trace 出力なし。

## 根本原因分析

`analyze_skill` phase は `input: user_message | eval_builder_request` という union input を宣言している。
`_union_schema()` がこれを `{"anyOf": [{...user_message...}, {...eval_builder_request...}]}` に変換する。

`infer_llm_visible_schema()` が preprocessor step の `into: data._prep` を検証する際、
`_require_parent_exists(schema, "data._prep", ...)` を呼び出す。
この関数は `_get_at_path(schema, "data")` を試みるが、
`anyOf` トップレベルには `properties` がなく `data` が見つからない → `PreprocessorTypeError` で失敗。

```python
# preprocessor_typing.py:22-38
def _get_at_path(schema: dict, path: str) -> Any:
    parts = path.split(".")
    cur = schema
    for part in parts:
        props = cur.get("properties")
        if not isinstance(props, dict) or part not in props:
            raise PreprocessorTypeError(...)  ← anyOf スキーマでここに到達
        cur = props[part]
```

単一 input (user_message only) の場合は `{"type":"object","properties":{"type":..., "data":...}}` が
直接返されるため `data` が見つかる。union (anyOf) の場合は top-level に `properties` がなく失敗する。

**これは `e6de782` の union input 導入によって発生した新 regression bug**。
union input (`user_message | eval_builder_request`) を `analyze_skill` に追加した時点で
`preprocessor_typing.py` が anyOf スキーマを扱えないことが顕在化した。

### 再現確認

```python
from reyn.compiler.preprocessor_typing import infer_llm_visible_schema
from reyn.schemas.models import PythonStep

step = PythonStep(type='python', module='...', function='compute_paths',
                  into='data._prep', output_schema={...})

# 単一 input → OK
single = {"type":"object","properties":{"type":{...},"data":{...}}}
infer_llm_visible_schema(single, [step], {})  # OK

# union (anyOf) → ERROR
union = {"anyOf": [{...user_message...}, {...eval_builder_request...}]}
infer_llm_visible_schema(union, [step], {})
# → PreprocessorTypeError: 'into' parent path 'data' not found in schema
```

## 6 軸評価

| 軸 | 評価 | 備考 |
|---|---|---|
| 応答品質 | N/A | LLM 未到達 (コンパイル失敗) |
| 意図解釈 | N/A | LLM 未到達 |
| 待ち時間 | N/A | コンパイルが即時失敗 (< 1s) |
| 見せ方 | NG | 技術的 error message がそのまま表示 |
| エラー UX | NG | user には不透明な DSL compile error |
| state 整合性 | OK | `.reyn/` 作成なし (LLM calls なし) = 0 token |

## prediction 評価

事前 prediction (分布形式):
- internal metric (5b): **80% verified / 15% inconclusive / 5% refuted**

実際の verdict: **refuted**

top probability category は verified (80%) だったが、実際は refuted (5%)。
**prediction MISS** (top probability が大幅に外れ)。

### MISS の理由

「構造データ直送なので route 段階の不確実性なし」という前提が正しかったが、
それより前の DSL コンパイル段階に別の bug が存在した。
`e6de782` の union input 導入が `preprocessor_typing.py` の anyOf 非対応を
顕在化させた。この regression は事前に想定できなかった。

### 5a vs 5b 比較 (verdict section 追記)

| 項目 | 5a (自然言語) | 5b (構造データ) |
|---|---|---|
| 失敗段階 | router LLM 応答 (実行時) | DSL コンパイル (起動前) |
| 失敗理由 | skill 名 hallucination | preprocessor_typing anyOf bug |
| LLM calls | 1 call (router) | 0 calls |
| .reyn 作成 | YES | NO |
| cost | $0.000193 | $0.000000 |
| G3 dedupe 観測 | YES (2 deduped) | N/A |
| union input 経路到達 | NO (skill 未起動) | NO (コンパイル前失敗) |
| eval.md 生成 | NO | NO |

**両経路とも union input の preprocessor には到達できなかった。**
5a は router の hallucination、5b は compile-time bug。

## 新規 finding: B7-S5b-NEW

**bug**: `preprocessor_typing.py` の `_get_at_path` / `_require_parent_exists` が
`anyOf` union スキーマを扱えない。

**impact**: `input: A | B` 形式の union input を持つ phase が
`into: data.*` の python preprocessor step を持つとき、
DSL コンパイルが常に失敗する。

**scope**: `e6de782` で union input を導入した `analyze_skill` phase が
直接的に影響を受ける。他の union input phase (他に存在すれば) も同様。

**fix direction**:
1. `_require_parent_exists` を `anyOf` 対応に拡張 (anyOf の各 branch で
   parent path が存在すれば OK とする)
2. または、`anyOf` スキーマでは parent path check を skip して
   runtime 検証に委ねる

## verdict 根拠

- DSL コンパイルが即時失敗: `preprocessor_typing.py` の anyOf 非対応
- eval_builder skill は一切起動しなかった
- union input の `eval_builder_request` 経路は未検証のまま
- eval.md は生成されなかった

verdict: **refuted** — 5b 経路では eval_builder が起動できなかった。
構造データ経路の検証が完全にブロックされた。

## next action

- `preprocessor_typing.py` の `_require_parent_exists` / `_get_at_path` を
  anyOf 対応に fix (= separate wave)
- fix 後に S5b を retest して union input `eval_builder_request` 経路を verify
- 5a の router hallucination と 5b の compile bug は独立した別 issue
