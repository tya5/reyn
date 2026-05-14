---
type: skill
name: ops_report
description: |
  直近期間 (default 1 週間) の Reyn 実行サマリーを events index または
  raw events log から生成する。

  Preferred path: `index_events` が実行済みであれば `recall(sources=["events"])`
  でデータを取得する。index が未登録または空の場合は `.reyn/events/*.jsonl`
  を直接スキャンする fallback path を使用する。
entry: collect
graph:
  collect: [summarize]
  summarize: []
final_output: ops_report_output
final_output_description: |
  LLM-contract artifact: 指定期間の実行統計サマリー、失敗スキルリスト、
  推奨アクション。summary_markdown が主出力（format="markdown" の場合）。
finish_criteria:
  - 集計データが取得された（recall または raw events fallback）
  - aggregate dict に total_runs が設定されている
  - summary_markdown が生成されている
search_hints:
  - "先週の実行サマリーを見たい"
  - "スキルの失敗率を確認したい"
  - "コスト上位スキルを調べたい"
  - "ops report を出力して"
  - "weekly operations report"
  - "last week skill execution summary"
  - "show me skill failures this week"
permissions:
  recall: allow
  file:
    read:
      - ".reyn/events/"
      - ".reyn/index/"
  python:
    - module: ./aggregate.py
      function: collect_aggregate
      mode: safe
      timeout: 30
    - module: ./aggregate.py
      function: aggregate_from_raw_events
      mode: safe
      timeout: 30
    - module: ./aggregate.py
      function: aggregate_from_recall_chunks
      mode: safe
      timeout: 10
phases:
  collect:
    input: ops_report_input
    preprocessor:
      steps:
        # Step 1: attempt recall from events index
        - type: run_op
          op:
            kind: recall
            query: "skill execution run summary failures errors"
            sources: ["events"]
            top_k: 50
          into: data.recall_result
          on_error: skip
        # Step 2: aggregate — uses recall if non-empty, falls back to raw events
        - type: python
          module: ./aggregate.py
          function: collect_aggregate
          into: data.aggregate
          mode: safe
          timeout: 30
---

## Overview

`ops_report` は直近 N 日間の skill 実行履歴を集計して人間が読めるレポートを
生成する stdlib スキルです。

## Execution flow

1. **Phase `collect`** (LLM):
   - OS preprocessor が `recall(sources=["events"])` で直近の実行チャンクを取得
   - recall 結果が空の場合は `aggregate.aggregate_from_raw_events` で
     `.reyn/events/*.jsonl` を直接スキャン（fallback path）
   - 集計 dict (`aggregate`) を `summarize` フェーズへ渡す

2. **Phase `summarize`** (LLM):
   - `aggregate` を受け取り、集計数値を元に人間が読める Markdown レポートを生成
   - 成功率・失敗スキル・タイムアウトエラーなどに基づいて推奨事項を生成

## Input

```bash
reyn run ops_report '{"period": "last-week"}'
reyn run ops_report '{"period": "last-7d", "focus": "failures", "format": "markdown"}'
reyn run ops_report '{"period_days": 30, "skills": ["swe_bench", "eval"]}'
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|-----|-----------|------|
| `period` | string | `"last-week"` | `last-week` / `last-7d` / `last-30d` / ISO 週番号 |
| `period_days` | integer | `7` | `period` の代替 — 日数で指定 |
| `focus` | string\|null | null | null=全体 / `"failures"` / スキル名 |
| `format` | string | `"markdown"` | `"markdown"` または `"json"` |
| `skills` | list\|null | null | 対象スキルを限定。null=全スキル |

## Output

`ops_report_output` アーティファクト:

- `period` — 人間可読な期間文字列（例: `"last 7 days"`）
- `total_runs` — 観測された実行回数
- `success_rate` — 成功率 (0.0–1.0)、実行なしの場合は null
- `failure_breakdown` — スキル別失敗件数リスト（降順）
- `cost_total_usd` — 期間合計コスト（Phase 1: null）
- `top_failing_skills` — 失敗上位スキル名リスト
- `recommendations` — 改善推奨リスト
- `summary_markdown` — Markdown 形式の narrative レポート

## Fallback detection strategy

`collect` phase preprocessor:

1. `recall(sources=["events"], top_k=50)` を試みる
2. 結果が空（chunk 数 = 0）または `SourceNotFound` エラーの場合、
   `aggregate_from_raw_events(".reyn/events", period_days, skills)` を実行
3. raw events も見つからない場合は `total_runs=0` の空集計を返す

フォールバックの判定ロジックは `aggregate.py` の `collect_aggregate` で
実装されますが、skill preprocessor はシンプルに recall → python step の
2 ステップ構成にして、python step 内でフォールバックを行います。

## See also

- `src/reyn/stdlib/skills/index_events/` — events source を populate するスキル
- `docs/concepts/rag.md` — `recall(sources=["events"])` の使い方
- FP-0009: `docs/deep-dives/proposals/0009-operational-intelligence.ja.md`
