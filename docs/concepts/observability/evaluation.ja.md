---
type: concept
topic: evaluation
audience: [human, agent]
---

# 評価インフラ

reyn は **構造化された評価インフラ** を P6 イベントログの上に構築しています。別途の観測レイヤーを追加するのではなく、reyn はすべてのスキル実行を潜在的な評価アーティファクトとして扱います。ライブデバッグ出力とクラッシュリカバリを支えるのと同じ append-only イベントストリームが、ゴールデンデータセットテスト、CI ゲート、外部トレース export の基盤になります。

**核心的な洞察:** すべての状態変化がすでにイベントである（P6）なら、評価とはそのログに対するクエリであり、新しい記録システムではない。

## なぜ評価インフラが重要か

### Goodhart's Law と報酬ハッキング

UC Berkeley（2026-04）は主要ベンチマーク 8 本で報酬ハッキングが可能であることを実証しました。ベンチマークが目標になった途端に、その指標を最適化することが本来の能力から乖離していく — Goodhart's Law の実例です。業界標準の対応策は **トレーサビリティ** です。「このランは何点だったか?」だけでなく「どのスキルバージョン、どのモデル、どのデータセットケース、どのフェーズで出した点数か?」が答えられることが重要です。

reyn の評価インフラはその問いに追加実行なしで答えるよう設計されています。`skill_version_hash`（FP-0006）はすべての `run_skill_started` イベントに記録されるため、バージョン間の事後比較は P6 ログ集計で実現できます。再実行は不要です。

### CI/CD ゲートは業界標準になった

Braintrust が CI/CD ゲートをデファクトスタンダードとして確立しました — スキルの pass rate が低下する PR は merge をブロックします。reyn の `reyn eval run` コマンドはしきい値未満で exit code 1 を返すため、そのまま CI ステップとして使用できます。

### データ主権要件

日本企業では、トレースデータを海外 SaaS に送信できないケースが多くあります。reyn の export adapter は Langfuse セルフホスト、OTLP（ローカル Jaeger、Grafana）、IETF Agent Audit Trail（ファイル出力）、ローカルファイルバックエンドをサポートします。外部依存は必須ではありません。

## 3 層アーキテクチャ

```
┌──────────────────────────────────────────────────┐
│  P6 イベントログ（実行ごとの append-only JSONL）    │  ← 基盤
│  .reyn/events/<run_id>.jsonl                      │
└──────────────────────────────────────────────────┘
             ↓ Component A: export adapter
┌──────────────────────────────────────────────────┐
│  Export adapter                                  │  ← 転送レイヤー
│  Langfuse / OTLP / IETF Audit Trail / file       │
└──────────────────────────────────────────────────┘
             ↓ Component B: reyn eval
┌──────────────────────────────────────────────────┐
│  reyn eval run / report / compare                │  ← オペレーター UI
│  ゴールデンデータセット実行 + CI しきい値ゲート   │
└──────────────────────────────────────────────────┘
```

### 第 1 層: P6 イベントログ（基盤）

すべての実行はすでに `.reyn/events/<run_id>.jsonl` に JSONL イベントログを生成します。これが P6 の保証です（[コンセプト: イベント](../runtime/events.md) 参照）。すべての状態変化はイベントを emit し、ログは append-only かつリプレイ可能です。評価インフラはこのログを読み取ります。新たな記録パスは追加しません。

IETF Agent Audit Trail ドラフトのフィールドは P6 イベント型に自然にマッピングされます:

| IETF フィールド | P6 マッピング |
|----------------|-------------|
| `identity` | `chain_id` / `skill_name` |
| `timing` | `timestamp`（全イベント共通） |
| `routing` | `run_skill_started.state_dir` |
| `parameters` | `tool_executed.op` + `tool_executed.args` |

### 第 2 層: export adapter（Component A）

スキル実行完了後に P6 イベントを外部評価プラットフォームへ非同期転送するアダプタ。export 失敗は警告のみ — P6 本体の書き込みは独立しており影響を受けません。

P7 遵守: アダプタは `type / timestamp / data` のみを読み取り、スキル固有のフィールド名を知りません。汎用イベントスキーマをそのまま転送します。スキルドメイン知識は外部ツールの rubric 内にあり、アダプタコードには存在しません。

サポートバックエンド: **Langfuse**（セルフホストまたはクラウド）、**OTLP**（OpenTelemetry — ローカル Jaeger、Grafana、Honeycomb）、**IETF Agent Audit Trail**（ファイル出力、ドラフト仕様）、**file**（ローカル `.reyn/traces/`、デフォルト）。

### 第 3 層: reyn eval（Component B）

オペレーター向けの操作インターフェース。`reyn eval run` はゴールデン JSONL データセットに対してスキルを実行し、`final_output` を `expected` と比較し、pass rate が `--threshold` 未満のとき 0 以外の終了コードで終了します。`reyn eval report` は過去の結果をサマリー表示します。`reyn eval compare` は P6 ログデータを使って 2 つのスキルバージョンを比較します — 追加実行は不要です。

## 4 コンポーネントマップ

| コンポーネント | 説明 | 依存関係 |
|-------------|-----|---------|
| **A** — Export adapter | P6 → Langfuse / OTLP / IETF / file | なし |
| **B** — `reyn eval` コマンド | ゴールデンデータセット実行 + CI ゲート + レポート | Component A（任意） |
| **C** — 回帰比較 | P6 ログからのバージョン間差分 | FP-0006 `skill_version_hash` |
| **D** — `judge_output` op | 任意フェーズから呼べる LLM スコアラー | なし |

Component A、B、D は FP-0006 に依存せず、Component C なしで使用できます。

## ポジショニング

**P7 遵守。** OS はスキル固有の rubric 内容を知りません。`judge_output` op（Component D）は呼び出し側スキルが供給する `target` パスと `rubric` 文字列を受け取ります。OS 側の実装はスコアとしきい値を通過したかどうかのみを知ります。スキルドメインの評価基準は OS コードに現れません。

**OSS セルフホスト対応。** Langfuse と Grafana/Tempo はどちらも OSS でセルフホスト可能です。ローカルファイルバックエンドは外部サービスを必要としません。reyn は評価に SaaS 依存を強制しません。

**IETF Agent Audit Trail 整合。** IETF ドラフト（draft-sharif-agent-audit-trail）は策定中です。reyn の export adapter は上記の P6 イベントマッピングを使ってドラフトのフィールド要件に sympathetic な出力を生成します。仕様はドラフトステータスとして exporter 設定に注記されます。

## 競合比較

| 機能 | Braintrust | Langfuse | Reyn |
|-----|-----------|---------|------|
| CI/CD eval ゲート | ✓ | ✓ | ✓（`reyn eval run --threshold`） |
| バージョン回帰比較 | ✓ | 一部 | ✓（P6 ログ集計、追加実行なし） |
| 外部 export | Braintrust SaaS のみ | Langfuse のみ | Langfuse / OTLP / IETF / file |
| セルフホスト対応 | ✗ | ✓ | ✓（全バックエンド） |
| IETF Agent Audit Trail | — | — | ✓（ドラフト準拠、Component A） |
| スキルフェーズ内 LLM スコアラー | — | — | ✓（`judge_output` op、Component D） |
| P7 OS/スキル分離 | 該当なし | 該当なし | ✓（rubric は常にスキルが供給） |

## Phase 1 スコープ

**含まれるもの（Component A、B、D）:**

- ファイル export バックエンド（デフォルト、設定不要）
- Langfuse、OTLP、IETF export バックエンド（`reyn.yaml` で設定）
- `reyn eval run` — CI しきい値ゲート付きゴールデンデータセット実行
- `reyn eval report` — 過去の結果サマリー
- `judge_output` op — 任意フェーズから呼べる LLM スコアラー
- Workspace 隔離 — eval 実行が本番 workspace を汚染しない保証

**延期（Component C — FP-0006 が前提）:**

- `reyn eval compare` — `skill_version_hash` を使ったバージョン間回帰比較

## 関連項目

- [ガイド: 評価インフラのセットアップ](../../guide/evaluation.md) — クイックスタート、export 設定、CI 連携
- [リファレンス: `reyn eval`](../../reference/cli/eval.md) — CLI フラグリファレンス
- [コンセプト: イベント](../runtime/events.md) — P6 イベントログ基盤
- [コンセプト: workspace](../runtime/workspace.md) — eval 実行の workspace 隔離
- [コンセプト: パーミッションモデル](../runtime/permission-model.md) — eval の非インタラクティブ事前承認
- [リファレンス: control-ir](../../reference/runtime/control-ir.md) — `judge_output` op スキーマ
- [FP-0007](../../deep-dives/proposals/0007-evaluation-infrastructure.md) — 設計根拠と実装仕様（内部向け）
