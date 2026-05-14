---
type: concept
topic: operational-intelligence
audience: [human, agent]
---

# Operational Intelligence

Reyn の P6 監査ログは、フェーズ遷移・ツール呼び出し・LLM 呼び出し・エラーといったすべての状態変化を append-only の JSONL ストリームとして記録します。これを ADR-0033 の RAG インフラと組み合わせると **operational intelligence** が生まれます。つまり Reyn エージェントは、自分自身の実行履歴を線形スキャンではなくセマンティック検索で参照できるようになります。`index_events` がイベントをインデックス化した後は、ドキュメント取得に使うのと同じ `recall` op が実行トレースにも機能します。

## アーキテクチャ

```
P6 events ──┐
            ├─► index_events (stdlib) ──► .reyn/index/events/ (sqlite)
            │                                      │
            │                                      ▼
            │                            recall(sources=["events"])
            │                                      │
            │           ┌──────────────────────────┼─────────────────┐
            │           ▼                          ▼                 ▼
            │      ops_report (skill)    FP-0006 collect_traces   debugging
            │      "週次サマリー"          "失敗パターン検索"        /chat 経由
            │
            └─► (index 未使用時の ops_report raw fallback)
```

`index_events` は stdlib スキル — OS 変更は不要（P7 準拠）。`.reyn/events/*.jsonl` を読み込み、イベントを run 単位のチャンクにグルーピングして、共有の `SqliteIndexBackend` に書き込みます。インデックス後は、任意のスキルの任意フェーズから `recall(sources=["events"], query="...", top_k=N)` で実行履歴をクエリできます。

## run チャンク形式

イベントは JSONL の 1 行 = 1 イベントで保存されますが、operational intelligence における意味的なまとまりは **1 run**（`run_skill_started` から `run_skill_completed` まで）です。`index_events` は各 run を 1 つの構造化チャンクに変換します:

```
[run chunk]
skill: my_skill
version_hash: abc123...  ← 実行時の skill.md の sha256（FP-0006 A）
timestamp: 2026-05-10T09:15:00
status: success
duration_seconds: 43
phases: explore → plan → apply → verify → report
errors: []
tool_calls: grep(×3), read_file(×5), edit_file(×2), shell(×1)
cost_usd: 0.18
```

失敗した run は error 詳細を追加フィールドとして保持するため、「my_skill の verify フェーズの失敗パターン」といったクエリで適切なチャンクが取得できます。

## インクリメンタル indexing

`index_events` は最終インデックス済みタイムスタンプを `.reyn/index/events_cursor` に保存します。次回実行時はそのタイムスタンプ以降のイベントのみ処理するため、ログが大きくなっても繰り返し実行のコストは低く保たれます。

```bash
# 初回 — すべてをインデックス
reyn run index_events

# 以降 — 前回カーソル以降の新規イベントのみ
reyn run index_events

# 開始日を指定
reyn run index_events --input '{"since": "2026-05-01T00:00:00"}'
```

## 実行履歴のクエリ

`index_events` 実行後は `recall` で `events` ソースが使えるようになります:

```yaml
# 任意のスキルの任意フェーズから
- op: recall
  query: "my_skill の verify フェーズの失敗パターン"
  sources: ["events"]
  top_k: 10
```

`/chat` からも直接クエリ可能:

```
> 先週 my_skill で何が問題だったか教えて
> 今月コストが高かったスキルはどれ？
> swe_bench が verify フェーズで失敗したランをすべて見つけて
```

## `skill_version_hash` と回帰検出

すべての `run_skill_started` イベントには `skill_version_hash`（実行時の `skill.md` ファイルの sha256 フル hex）が含まれます（FP-0006 Component A として着地）。このフィールドは `index_events` チャンクを経由して `reyn eval compare` で活用されます。

`reyn eval compare my_skill` は P6 ログを `skill_version_hash` でグルーピングしてバージョンごとの pass rate を算出します — 追加の実行は不要です:

```
Baseline:  sha:abc12345  72% pass（50 ラン中 36 通過）  2026-05-01 〜 2026-05-05
Candidate: sha:def67890  88% pass（50 ラン中 44 通過）  2026-05-05 〜 2026-05-15
Delta:     +16pp  /  回帰: なし
```

フル CLI リファレンスは [リファレンス: `reyn eval compare`](../reference/cli/eval.ja.md#reyn-eval-compare) を参照してください。

## `ops_report` — 既製の運用サマリー

`ops_report` stdlib スキルはカスタムクエリなしで週次サマリーを生成します:

```bash
reyn run ops_report
reyn run ops_report --input '{"period_days": 30}'
```

出力例:

```
[Weekly ops report 2026-W19]
実行スキル: 5 種類、合計 127 回
成功率: 91.3%（127 回中 116 回）
平均コスト: $0.21 / run
最高失敗スキル: swe_bench（10 回中 3 回失敗）
  → 主な原因: verify フェーズでのテスト実行タイムアウト
```

`index_events` が未実行の場合、`ops_report` は `.reyn/events/*.jsonl` の直接読み取りにフォールバックします。大規模ログではインデックス利用パスの方が大幅に高速です。

## RAG Phase 1 との関係

`index_events` は `index_docs` の run ログ特化バリアントです。どちらも同じ `SqliteIndexBackend` に書き込みます。違いはチャンク単位とインクリメンタルの仕組みのみです:

| | `index_docs` | `index_events` |
|---|---|---|
| 入力 | ドキュメントファイル（`.md`、`.txt` など） | P6 event JSONL |
| チャンク単位 | パッセージ（LLM が戦略を決定） | 1 run（固定） |
| インクリメンタル | ファイルハッシュの変化 | タイムスタンプカーソル（`.reyn/index/events_cursor`） |
| バックエンド | `SqliteIndexBackend`（共有） | `SqliteIndexBackend`（共有） |

## 関連情報

- [FP-0009: Operational Intelligence](../deep-dives/proposals/0009-operational-intelligence.ja.md) — 設計の詳細
- [FP-0006: スキル自己改善](../deep-dives/proposals/0006-skill-self-improvement.ja.md) — `skill_version_hash` の契約
- [FP-0007: 評価インフラ](../deep-dives/proposals/0007-evaluation-infrastructure.ja.md) — `reyn eval compare` の設計
- [コンセプト: RAG](rag.ja.md) — 基盤となる index/recall プリミティブ
- [コンセプト: Events](events.ja.md) — P6 イベントログの構造
- [リファレンス: `reyn eval compare`](../reference/cli/eval.ja.md#reyn-eval-compare) — CLI リファレンス
