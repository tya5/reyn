---
type: concept
topic: operational-intelligence
audience: [human, agent]
---

# Operational Intelligence

Reyn の P6 監査ログは、フェーズ遷移・ツール呼び出し・LLM 呼び出し・エラーといったすべての状態変化を append-only の JSONL ストリームとして記録します。これを ADR-0033 の RAG インフラと組み合わせると **operational intelligence** が生まれます。つまり Reyn エージェントは、自分自身の実行履歴を線形スキャンではなくセマンティック検索で参照できるようになります。ドキュメント取得に使うのと同じ `recall` op は、イベントログを他のコーパスと同じ [`embed_and_index()`](rag.ja.md) プリミティブで source に index した後なら実行トレースにも機能します — イベント専用の indexing skill は存在しません。

## アーキテクチャ

```
P6 events ──┐
            ├─► 自作の indexing step ──► embed_and_index(source="events") ──► .reyn/cache/index/events/ (sqlite)
            │                                                                        │
            │                                                                        ▼
            │                                                          recall(sources=["events"])
            │                                                                        │
            │                                       ┌────────────────────────────────┼─────────────────┐
            │                                       ▼                                ▼                 ▼
            │                            自分で書く分析フェーズ         FP-0006 collect_traces      debugging
            │                            (バンドルされた"週次サマリー"は無し) "失敗パターン検索"        /chat 経由
            └─► index が存在しない場合は raw ファイル読み取り(`.reyn/events/*.jsonl`)にフォールバック
```

イベントログの indexing はバンドルされた skill ではありません — `.reyn/events/*.jsonl` を読み、イベントを run 単位のチャンクにグルーピングし、他のコーパスと同じように `embed_and_index(chunks, source="events", ...)` を呼ぶ `python` step を自分で書きます（[コンセプト: RAG — クイックスタート](rag.ja.md#クイックスタート) 参照）。index 後は任意のフェーズから `recall(sources=["events"], query="...", top_k=N)` で実行履歴をクエリできます。

## run チャンク形式

イベントは JSONL の 1 行 = 1 イベントで保存されますが、operational intelligence における意味的なまとまりは **1 run**（`session_started` から `session_completed` まで）です。`embed_and_index` を呼ぶ前に、各 run を 1 つの構造化チャンクにグルーピングします:

```
[run chunk]
agent: my_agent
timestamp: 2026-05-10T09:15:00
status: success | failed | aborted
tool_calls: grep(×3), read_file(×5), edit_file(×2), shell(×1)
cost_usd: 0.18   ← run 全体の llm_response_received.cost_usd を集計
```

実際に使えるフィールドは、どの P6 イベントタイプをチャンクに折り込むかによります — 現行のイベント分類（`session_started`/`session_completed`、`turn_started`/`turn_completed`、`llm_response_received`、tool-call 系イベント）は [コンセプト: Events](../runtime/events.ja.md) を参照してください。「1 run = 1 イベント」の既製サマリーイベントは存在しません。run チャンクを作るには、自分の indexing step で session/turn の境界イベントを自分で集計する必要があります。

失敗した run は error 詳細を追加のチャンクメタデータとして保持し、「my_agent の失敗パターン」といったクエリで適切なチャンクが取得できるようにしてください。

## インクリメンタル indexing

`embed_and_index` の `mode="append"`（[コンセプト: RAG — 制限事項](rag.ja.md#制限事項) 参照）を使い、最終インデックス済みタイムスタンプを自分で追跡してください（例: `.reyn/cache/` 配下のカーソルファイル）。そうすれば繰り返し実行時に前回実行以降のイベントのみを処理できます。

## 実行履歴のクエリ

source を index した後は、任意のフェーズから `recall` でクエリできます:

```yaml
- type: run_op
  op:
    kind: recall
    query: "my_agent の失敗パターン"
    sources: ["events"]
    top_k: 10
  output_name: trace_summary
```

`/chat` からも直接クエリ可能:

```
> 先週何が問題だったか教えて
> ファイル編集中に失敗したランをすべて見つけて
```

## RAG Phase 1 との関係

イベントログの indexing は、ドキュメントの indexing とまったく同じ `embed_and_index()` エントリーポイントを使います（[コンセプト: RAG](rag.ja.md) 参照）— 違いは何を chunk にするか（パッセージ単位ではなく run 単位）と、インクリメンタル進捗の追跡方法（`content_hash` の重複排除ではなくタイムスタンプカーソル）だけです。

## スケジューリング

定期的な indexing（およびその上に構築するレポート機能)はバンドル機能ではありません — `reyn.yaml` の `cron:` ジョブは skill 呼び出しではなく named **agent** へメッセージを配送します（`to`/`message`)。イベント index を定期的に最新に保つには、indexing step を実行するタスクを持つ agent が必要です:

```yaml
cron:
  jobs:
    - name: reindex_events_hourly
      to: ops_agent
      message: "events source を再インデックスし、前回実行以降の失敗をサマリーして"
      schedule: "0 */6 * * *"   # 6時間ごと
      enabled: true
```

現行のジョブスキーマ、実行モード、状態確認コマンドは [Reference: `reyn cron`](../../reference/cli/cron.md) と [Reference: `reyn.yaml`](../../reference/config/reyn-yaml.md) を参照。

## 関連情報

- [FP-0009: Operational Intelligence](../../deep-dives/proposals/0009-operational-intelligence.ja.md) — 元の設計根拠（skill-word 除去より前の記述。ここで説明したプリミティブは現行、skill ベースの例は現行ではない)
- [コンセプト: RAG](rag.ja.md) — 基盤となる index/recall プリミティブ
- [コンセプト: Events](../runtime/events.ja.md) — P6 イベントログの構造と現行のイベント分類
