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
    # R-PURE-MODE wave 3a: dispatch_aggregate is pure — aggregates recall chunks
    # inline via aggregate_from_recall_chunks. No glob/os/pathlib imports.
    # Declared mode: safe; 99% hot path (recall hit) runs here.
    - module: ./aggregate_pure.py
      function: dispatch_aggregate
      mode: safe
      timeout: 10

    # R-PURE-MODE wave 3a: collect_aggregate_fallback is honestly mode: unsafe
    # (globs .reyn/events/*.jsonl). Runs unconditionally but no-ops if upstream
    # dispatch_aggregate already produced recall stats (_path=recall).
    - module: ./aggregate.py
      function: collect_aggregate_fallback
      mode: unsafe
      timeout: 30

    # aggregate_pure.py imports only PURE_STDLIB_ALLOWLIST entries (collections,
    # typing) — no glob/os/pathlib. R-PURE-MODE-REDEFINE wave 2 Class C fix:
    # the function was already pure; extraction from aggregate.py unblocks the
    # honest mode: safe declaration.
    - module: ./aggregate_pure.py
      function: aggregate_from_recall_chunks
      mode: safe
      timeout: 10

    # aggregate.py imports `glob` at module level for .reyn/events/*.jsonl
    # discovery. `glob` is intentionally outside the safe-mode allowlist
    # (operator filesystem-state ingress per R-PURE-MODE). Stdlib unsafe is
    # auto-allowed by `reyn run` via run.py:104-106.
    - module: ./aggregate.py
      function: aggregate_from_raw_events
      mode: unsafe
      timeout: 30

    # Back-compat wrapper — kept for tests and direct callers. Delegates to
    # dispatch_aggregate + collect_aggregate_fallback internally.
    - module: ./aggregate.py
      function: collect_aggregate
      mode: unsafe
      timeout: 30
---

## Overview

`ops_report` は直近 N 日間の skill 実行履歴を集計して人間が読めるレポートを
生成する stdlib スキルです。

## Execution flow

1. **Phase `collect`** (LLM):
   - OS preprocessor が `recall(sources=["events"])` で直近の実行チャンクを取得
   - `dispatch_aggregate` (mode: safe) が recall chunks を inline 集計し、
     `{_path: "recall", ...stats}` を返す（hot path、99%）。
     chunks が空の場合は `{_path: "needs_fallback"}` sentinel を返す。
   - `collect_aggregate_fallback` (mode: unsafe) が _path sentinel を検査し、
     recall 済みなら no-op（sentinel strip のみ）。needs_fallback なら
     `.reyn/events/*.jsonl` を直接スキャン（fallback path、1%）
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

## Fallback detection strategy (R-PURE-MODE wave 3a: 3-step chain)

`collect` phase preprocessor:

1. `recall(sources=["events"], top_k=50)` を試みる（on_error: skip）
2. `dispatch_aggregate` (mode: safe) が recall 結果を検査:
   - chunks ≥ 1 → `aggregate_from_recall_chunks` で inline 集計、
     `{_path: "recall", ...stats}` を返す（hot path）
   - chunks = 0 / recall skip → `{_path: "needs_fallback", period_days, skills}` sentinel
3. `collect_aggregate_fallback` (mode: unsafe) が sentinel を検査:
   - _path = "recall" → sentinel strip のみ（no-op）
   - _path = "needs_fallback" → `aggregate_from_raw_events(".reyn/events", ...)` 実行
   - raw events も見つからない場合は `total_runs=0` の空集計を返す

注: `when:` 条件分岐は preprocessor ステップ未サポートのため、unconditional
3-step chain + internal sentinel detection パターンを採用。
`collect_aggregate` (back-compat wrapper) は既存テスト・直接呼び出し元向けに残存。

## See also

- `src/reyn/stdlib/skills/index_events/` — events source を populate するスキル
- `docs/concepts/rag.md` — `recall(sources=["events"])` の使い方
- FP-0009: `docs/deep-dives/proposals/0009-operational-intelligence.ja.md`
