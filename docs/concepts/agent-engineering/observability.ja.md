---
type: concept
topic: architecture
audience: [human, agent]
---

# Observability

何が起きたかを事後にも、ライブにも検査・再構築できるだけの痕跡を残すこと。目標は「何かおかしいと感じたとき、デバッガーセッションで再構築するのではなく、指し示せるログがあること」です。

## Reyn の実装方法

### P6 audit-event ログ

Reyn 自身の OS が引き起こすすべての状態変化は、JSONL ストリーム(`.reyn/events/<run_id>.jsonl`)に audit-event を発行します — ライブデバッグ出力、`reyn events` リプレイ、eval 分析を支える単一のチャンネルです。後付けの独立したロガー、トレーサー、テレメトリーフックは存在しません。完全なモデルは [Events](../runtime/events.ja.md) を参照してください。このレンズが鋭く保つべき重要な区別を含みます: audit-event(observability のトレース)は WAL-event(crash-recovery/time-travel の基盤)や hook-event(外部リアクティビティトリガー)とは別物です — これらを混同することは文体上の細かい話ではなく、本物のカテゴリーエラーです(`CLAUDE.md` の Constitution 節がこの 3 つを明示的に区別しています)。

### `chain_id` — リクエストをホップをまたいで追跡する

1 つのトップレベルのユーザー送信が `chain_id`(uuid4 hex)を発行し、それが生成する agent 間の各ホップに変更されずに伝播します。1 つの論理的リクエストのエージェント横断的な再構築は、各 agent 自身の `events.jsonl` に対する `grep <chain_id>` です — 識別子がメッセージ自体とともに移動するため、集中トレースコレクターは不要です。

### `reyn events` リプレイ

保存された audit-event ログを、LLM を再呼び出しすることなく、ライブランと同じレンダリングでコンソールに再生します — ログ自体がデバッグツールであり、その補助ではありません。`--filter TYPE` は 1 つの event 種に絞り込みます(例: `--filter permission_denied` で拒否された op に直接ジャンプ)。

### ライブ audit chip(inline CUI)

inline CUI のステータス chip バー(Agents / Cost / Model / Tools / MCP / Skills / Hooks / Pipes / Cron / Tasks)は、事後のリプレイでのみ利用可能なのではなく、この同じ audit トレースをライブかつインラインで表面化したものです — オペレーターは P6 ログが記録するのと同じ状態をリアルタイムで見ます。

## まだ薄い部分

run をまたいだ集計は薄い部分です: audit-event ログの上に構築された run 横断のトレンドビューやダッシュボードはありません — 各 run の `.jsonl` ファイルは完全で自己完結した記録ですが、それらをフリートレベルの observability に集約するのはオペレーター自身のツーリングに委ねられています(データは他のツールにフィードするのに十分な構造を持っています)。event 種だけでなく LLM 呼び出し自体のペイロードレベルのトレース検査は、別の補完的なサーフェスです — [`docs/reference/dogfood-tracing.md`](../../reference/dogfood-tracing.md) を参照してください。

## 関連情報

- [Concepts: events](../runtime/events.ja.md) — audit-event/WAL-event/hook-event モデルの全体
- [リファレンス: events](../../reference/runtime/events.md) — 完全な audit-event 分類
- [リファレンス: `reyn events`](../../reference/cli/events.md) — リプレイ CLI
- [reliability-engineering.md](reliability-engineering.md) — このレンズと混同してはならない WAL ベースの基盤
