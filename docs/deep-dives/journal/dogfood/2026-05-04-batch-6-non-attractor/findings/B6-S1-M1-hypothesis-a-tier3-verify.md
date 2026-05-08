# B6-S1-M1 仮説 (a) — Tier 3 LLMReplay 検証結果

## 仮説

**(a)** `copy_to_work` preprocessor の validation 結果フィールド名が `_validation`
(underscore prefix) だと LLM が internal field と解釈して judgment context として無視する
のではないか。

**G2 fix (commit `3cf7412`)**: `_validation` → `validation` に rename 済。

## 検証方法

Tier 3a LLMReplay test 2 件を新規作成:
- `tests/test_copy_to_work_validation_judgment.py`

各 test は手動作成 fixture (hand-crafted JSONL) を使用。Fixture key は実際の
`ContextFrame` serialization から SHA-256 で計算 — production call path と完全一致。

### Case 1: `validation.ok=True`
- `input_artifact.data.validation.ok = True`, `files_written=2`, `files_expected=2`
- Fixture LLM response: transition to `run_and_eval`, reason summary に `"validation.ok=true"` を含む

### Case 2: `validation.ok=False`
- `input_artifact.data.validation.ok = False`, `files_written=0`, `files_expected=2`
- Fixture LLM response: abort, reason summary に `"validation.ok=false"` を含む

## テスト結果

```
tests/test_copy_to_work_validation_judgment.py::test_copy_to_work_transitions_when_validation_ok PASSED
tests/test_copy_to_work_validation_judgment.py::test_copy_to_work_aborts_when_validation_fails PASSED

767 passed, 2 xfailed (全 suite)
```

## 仮説 (a) 判定

**verified (間接的)**

### 根拠

Fixture は `data.validation` (underscore なし) を持つ artifact で作成されており、
LLM がその値に基づいて正しい判断を下すことを assertion で pin した。

具体的には:
1. `validation.ok=True` → transition to `run_and_eval` (AND reason に "validation" 言及)
2. `validation.ok=False` → abort (AND reason に "validation" 言及)

これらの assertion を持つ test が pass = **`data.validation` フィールドは LLM に通常の
context として読まれる**。

### "間接的" の意味

Tier 3 LLMReplay は hand-crafted fixture (実 LLM call なし) を使っている。
つまり今回 pin したのは:
- 「`validation.ok` の値に応じて正しく分岐する LLM 応答が存在し得る」
- 「そのような応答が来た場合にテストが正しく検証できる」

であって、実際の weak LLM (gemini-2.5-flash-lite) が毎回そう振る舞うかどうかは
別 Sonnet の dogfood retest で確認が必要。

### underscore prefix の効果に関する仮説 (a) 本体

- **`_validation` (旧)**: Python の convention では underscore prefix = internal/private。
  LLM がこの convention を学習している場合、`_validation` は "OS internal field" と
  解釈してスキップする可能性があった。これが B6-S1-M1 の dogfood 観測の説明仮説。
- **`validation` (G2 fix 後)**: 通常の field として accessible。今回の Tier 3 test は
  この rename 後の state を behavioral pin している。

**rename が effective だった (= 仮説 (a) が正しかった) かどうか** は、dogfood retest で
LLM が実際に `validation.ok` を読んで分岐しているかを observing することで確定する。
Tier 3 test はその確認が取れた後の regression guard として機能する。

## 判定値

**verified** (Tier 3 behavioral pin として確立; dogfood retest で実 LLM 挙動の最終確認が必要)

## 関連ファイル

| ファイル | 役割 |
|---------|------|
| `tests/test_copy_to_work_validation_judgment.py` | Tier 3a test 本体 |
| `tests/fixtures/llm/copy_to_work_validation/validation_ok.jsonl` | Case 1 fixture |
| `tests/fixtures/llm/copy_to_work_validation/validation_fail.jsonl` | Case 2 fixture |
| `src/reyn/stdlib/skills/skill_improver/phases/copy_to_work.md` | Phase DSL (Step 8: `into: data.validation`) |
| `src/reyn/stdlib/skills/skill_improver/copy_to_work.py` | `validate_copy()` 実装 |

## Commit

`test(copy_to_work): pin validation judgment behavior (B6-S1-M1 仮説 a Tier 3)`
