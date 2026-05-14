# FP-0028: プラン進捗 UX — ステータスメッセージへのステップ説明の表示

**Status**: proposed
**Proposed**: 2026-05-14
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

プラン実行中にユーザーへ発行されるステータスメッセージは、現在内部の `step.id`（例: `"plan step 2/4 done (s3)"`）を露出している。`s3` がどのゴールに対応するかユーザーには分からず、意味をなさない。id の代わりに人間が読めるステップの説明文を表示することで、進捗が可視化され、プランが正しいことに取り組んでいるという確認をユーザーに与えられる。

---

## Motivation

### 現在の出力

```
plan step 1/4 done (s1)
plan step 2/4 done (s2)
plan step 3/4 done (s3)
```

ユーザーには `s1`、`s2`、`s3` が何を意味するか分からない。プランのゴールが「認証フローを分析する」だったとして、プランが実際にそのゴールへ向けて進んでいるかどうかが不可視。

### 期待される出力

```
plan step 1/4: auth.py を読み JWT デコードロジックを特定
plan step 2/4: session.py を読みセッションライフサイクルをマッピング
plan step 3/4: middleware.py で認証パスを確認
```

ユーザーは完了した内容を確認でき、プランが正しく進んでいるか判断できる。

---

## Proposed implementation

### 1. `execute_plan` のステータス emit を更新（planner.py）

現在（行 ~855）:

```python
status_text = f"plan step {n_done}/{n_total} done ({step.id})"
```

提案:

```python
desc_preview = (step.description or step.id)[:60]
status_text = f"plan step {n_done}/{n_total}: {desc_preview}"
```

`step.description` はプランナー LLM によってすでに設定されている — 人間が読めるタスク説明文。`:60` の切り詰めでメッセージをコンパクトに保つ。`description` が想定外に空の場合 `step.id` にフォールバックすることで既存の挙動を維持する。

---

## 対象ファイル

| ファイル | 変更内容 |
|---|---|
| `src/reyn/chat/planner.py` | `execute_plan` のステータスメッセージ |

---

## Dependencies

なし。`PlanStep.description` はすでに設定されている。

---

## Cost estimate

SMALL — `planner.py` の 1 行変更のみ。

---

## Verification

1. 3 ステップ以上のプランを実行 → ステータスメッセージが `(sN)` でなく切り詰められた説明文を表示する。
2. ステップの description が空の場合（エッジケース）→ step.id にグレースフルにフォールバックする。

---

## Related

- `src/reyn/chat/planner.py` — `execute_plan`、`PlanStep.description`
- FP-0025 (`0025-planner-narration-and-sp-fixes.ja.md`) — 本 FP が拡張する合成フローを導入
