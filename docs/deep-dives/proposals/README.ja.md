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
| [0002](0002-index-docs-recall-docs.md) | index_docs / recall_docs — 統合ドキュメント検索スキル | done (ADR-0033 Accepted、 1e6f153) | LARGE |
| [0003](0003-budget-exceed-user-approval.md) | budget 超過時のユーザー許諾・再開フロー | proposed | SMALL |
| [0004](0004-safety-config-ux.md) | safety 設定 UX 改善 — 概念レイヤーとの整合 | proposed | MEDIUM |
| [0005](0005-safety-as-checkpoint.md) | safety limit をチェックポイントとして扱う — Permission モデルとの統合 | proposed | LARGE |
| [0006](0006-skill-self-improvement.md) | スキル自己改善 — 実行トレース駆動 + バージョン管理 + ロールバック | proposed | MEDIUM |
| [0007](0007-evaluation-infrastructure.md) | Agent 評価インフラ — P6 トレース export + スキル回帰評価 | proposed | LARGE |
| [0008](0008-swe-bench-integration.md) | SWE-bench 参加インフラ — stdlib スキル + バッチ実行 | proposed | LARGE |
| [0009](0009-operational-intelligence.md) | Operational Intelligence — イベントログの RAG インデックス化 | proposed | MEDIUM |
| [0010](0010-rag-routing.md) | RAG ルーティング — スキルカタログ + ルーティング履歴の semantic pre-filter | proposed | MEDIUM |
| [0011](0011-remove-narrator.md) | `skill_narrator` 廃止 — スキル結果の narrate をルーターに委ねる | proposed | SMALL |
| [0012](0012-async-skill-execution.md) | スキル/エージェント/プランの非同期実行 — 長時間タスクのノンブロッキング化 | done (LANDED 2026-05-10, commit `c9e79d6`) | LARGE |
| [0013](0013-unified-inbox-outbox-transport.ja.md) | 統合 Inbox/Outbox Transport 抽象化 — CUI vs MCP/A2A の skew を解消 | accepted (ADR-A green-light 2026-05-11) | LARGE |
| [0014](0014-python-step-api-package.ja.md) | Python step 用 API package + mode 改名 (pure→safe, trusted→unsafe) | partial-landed 2026-05-11 (A–F + ADR-G Phase 1; commits `5b435e1`/`b405975`/`527e11f`) | MEDIUM |
| [0015](0015-fine-grained-python-step-audit.ja.md) | Python step の per-call audit (双方向 RPC) | deferred (= enterprise audit 要件発生待ち) | MEDIUM |
| [0016](0016-agent-authentication.ja.md) | エージェント認証 — OAuth 委譲・トークンライフサイクル・MCP 認証ヘッダー | Component A 着地 2026-05-11 (commit `ec94a06`); B/C/D/E proposed | LARGE |
| [0017](0017-sandboxed-execution.ja.md) | サンドボックス実行 — ポリシー/バックエンド抽象化と exec op の非推奨化 | Component A+D 着地 2026-05-11 (commit `ddf2d05`); B/C/E proposed | MEDIUM |
| [0018](0018-event-store-backend.ja.md) | Event Store バックエンド抽象化 — JSONL / SQLite / DuckDB（優先度: LOW） | proposed | MEDIUM |
