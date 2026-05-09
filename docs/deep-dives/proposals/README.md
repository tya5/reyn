# Feature Proposals

機能実装の提案をまとめるディレクトリ。

ADR（`decisions/`）は「なぜその設計を選んだか」の記録。
このディレクトリは「何を実装すべきか」の提案。

---

## ファイル命名規則

```
NNNN-<kebab-case-title>.md
```

例: `0001-a2a-task-lifecycle.md`

---

## ステータス

| 値 | 意味 |
|---|---|
| `proposed` | 提案済み、未着手 |
| `accepted` | 実装決定 |
| `in-progress` | 実装中（PR 番号を記載） |
| `done` | 実装完了（commit/PR を記載） |
| `deferred` | 保留（理由を記載） |
| `rejected` | 却下（理由を記載） |

---

## フォーマット

各提案ファイルには以下のセクションを含める：

```markdown
# FP-NNNN: タイトル

**Status**: proposed
**Proposed**: YYYY-MM-DD
**Author**: (セッション名 or 担当者)

## Summary
1 段落で何を・なぜ実装するか。

## Motivation
ユースケース・背景・競合との比較など。

## Proposed implementation
実装方針の概要（詳細設計は ADR に委譲）。

## Dependencies
前提となる実装・PR。

## Cost estimate
SMALL / MEDIUM / LARGE（根拠付き）。

## Related
関連 ADR・PR・docs へのリンク。
```

---

## Index

| # | タイトル | Status | コスト |
|---|---|---|---|
| [0001](0001-a2a-task-lifecycle.md) | A2A task lifecycle — ask_user / push notification 対応 | proposed | MEDIUM |
| [0002](0002-index-docs-recall-docs.md) | index_docs / recall_docs — 統合ドキュメント検索スキル | in-progress (ADR-0033) | LARGE |
