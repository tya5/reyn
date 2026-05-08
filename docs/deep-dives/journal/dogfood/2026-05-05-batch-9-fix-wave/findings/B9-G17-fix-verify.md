---
id: B9-G17
batch: 9
date: 2026-05-05
bug_ref: B8-NEW-6 / giveup-tracker G17
status: resolved
---

# B9-G17: `_extract_skill_name` の unknown artifact_type 対応

## Diagnosis

`invoke_skill(name="eval_builder", input={"target_skill": "direct_llm"})` のように
LLM が `input` dict に `type` フィールドを含めない場合、OS が artifact を
`artifact_type="unknown"` に分類する。

旧 `_extract_skill_name` は 2 分岐:
1. `artifact_type == "eval_builder_request"` → `data.target_skill` 直参照
2. else → `data.text` に regex 適用 (user_message 想定)

`artifact_type="unknown"` は case 2 に落ち、`data` に `text` キーがないため
`data.get("text", "") == ""`、regex がマッチせず ValueError。

B8-S5b の observation log で確認:
```
"kind": "ValueError",
"error": "Cannot extract skill name from user_message text: ''. ..."
```

## Fix

`src/reyn/stdlib/skills/eval_builder/analyze_skill_resolver.py` の
`_extract_skill_name` を inverted structure に変更:

**変更前**: artifact_type で分岐 → 型判定が先
**変更後**: `data` に `"target_skill"` キーが存在するかを FIRST チェック → 型不問

```python
# Priority 1: target_skill field present (any artifact type, incl. "unknown")
if "target_skill" in data:
    name = str(data["target_skill"]).strip()
    if not name:
        raise ValueError(...)
    return name

# Priority 2: natural-language text fallback
text = str(data.get("text", "")).strip()
...
```

この設計の根拠:
- OS は skill-specific field 名 (`target_skill`) を知るべきでない (P7)
  → OS 側で `unknown` を `eval_builder_request` に再分類する案は却下
- `target_skill` の存在チェックは skill-side で完結 → P7 violation なし
- artifact_type に依存しないため、将来 LLM が別の type 名を使っても機能する

## Tests

`tests/test_eval_builder_path_resolution.py` に Tier 2 テスト 5 件追加:

| テスト名 | ガードする invariant |
|---|---|
| `test_extract_skill_name_unknown_type_with_target_skill` | type="unknown" + target_skill → 正常解決 (新挙動) |
| `test_extract_skill_name_empty_type_with_target_skill` | type field なし + target_skill → 正常解決 |
| `test_extract_skill_name_eval_builder_request_still_works` | 旧 eval_builder_request path の regression guard |
| `test_extract_skill_name_unknown_type_text_only_falls_back_to_regex` | target_skill なし + text あり → regex fallback 維持 |
| `test_extract_skill_name_unknown_type_no_target_skill_no_text_raises` | どちらもない → ValueError (エラー境界) |

## Per-fix retest plan

| Check | Result |
|---|---|
| Fix ファイル | `analyze_skill_resolver.py:_extract_skill_name` のみ変更 |
| 関係ファイル非変更 | OS 層 (`op_runtime/`, `models.py`) 変更なし (P7 保持) |
| テスト suite | 991 passed, 2 xfailed (baseline 986 + 5 新規) |
| Regression | なし |

## giveup-tracker 更新

G17 status: **active → resolved** at this commit
