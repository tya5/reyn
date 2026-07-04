---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn cron]
---

# `reyn cron`

cron スケジュール駆動のジョブを実行・確認します。ジョブは `reyn.yaml` の `cron.jobs` に宣言し、スケジューラーは各有効ジョブを cron 式のタイミングでメッセージ（`to` + `message`）として named agent の inbox に配送します（`sender="cron:<name>"` 付き）。

## 概要

```
reyn cron run
reyn cron list
reyn cron status
```

## 説明

`reyn cron` は時刻トリガーによるスキル実行を管理します。オペレーターは `reyn.yaml` にジョブを宣言し、`reyn cron run` はフォアグラウンドでスケジューラーを起動して各有効ジョブを設定されたインターバルで実行します。`reyn cron list` と `reyn cron status` はスケジューラーを起動せずにジョブ一覧を確認するためのコマンドです。

## サブコマンド

### `run`

cron スケジューラーをフォアグラウンドで起動します。Ctrl-C を押すまでブロックします。

```
reyn cron run
```

**動作:**

1. `reyn.yaml` の `cron.jobs` を読み込みます。
2. 各有効ジョブについて、cron 式から次回実行時刻を計算します。
3. 有効ジョブの一覧と次回実行時刻を含む起動バナーを表示します。
4. 各ジョブを独立した asyncio タスクで実行します。タスクは次回実行時刻までスリープし、時刻になったらジョブのメッセージを対象 agent の inbox に push します（`sender="cron:<name>"`）。agent の router loop がそれを通常の attributed turn として処理します。スタンドアロン/フォアグラウンドモード（稼働中の `AgentRegistry` が無い場合）では配送はエラーになります — このモードは対象 agent が `reyn web` 側で稼働しているジョブに向いています。
5. Ctrl-C 時は実行中ジョブの完了を最大 5 秒待ってからクリーンに終了します。

**例:**

```bash
$ reyn cron run
Started cron scheduler with 2 enabled job(s):
  • morning_news       (0 9 * * *)     next: 2026-05-16T09:00:00+00:00
  • weekly_ops_report  (0 9 * * MON)   next: 2026-05-19T09:00:00+00:00
^C
Cron scheduler stopped.
```

**Exit codes:**

| コード | 意味 |
|------|------|
| `0` | スケジューラーが正常停止（Ctrl-C またはジョブ未設定）。 |
| `1` | 起動中に致命的エラー（例: `reyn.yaml` のパース失敗）。 |

### `list`

設定されている全 cron ジョブと次回実行予定時刻を表示します。スケジューラーは起動しません。

```
reyn cron list
```

**出力形式:**

```
NAME                     TO             SCHEDULE        ENABLED  NEXT RUN
morning_news             news_agent     0 9 * * *       true     2026-05-16T09:00:00+00:00
weekly_ops_report        ops_agent      0 9 * * MON     true     2026-05-19T09:00:00+00:00
```

ジョブが設定されていない場合:

```bash
$ reyn cron list
(no jobs configured)
```

**Exit codes:**

| コード | 意味 |
|------|------|
| `0` | 常に（テーブルが空の場合も含む）。 |

### `status`

`reyn cron list` と同様ですが、`LAST RUN AT`・`LAST STATUS`・`LAST ERROR` の最終実行情報も表示します。

```
reyn cron status
```

> **v1 制限:** 最終実行状態はメモリ内にのみ保持されます。スタンドアロンで（= 実行中の `reyn cron run` セッションの外で）呼び出した場合、`last_run_*` フィールドはすべて `-` と表示されます。将来の Web モード API（`/a2a/agents/cron/status`）では稼働中スケジューラーの状態を照会できるようになる予定です。

**出力形式（スタンドアロンモード）:**

```
NAME                     TO             SCHEDULE        ENABLED  NEXT RUN                    LAST RUN AT   LAST STATUS   LAST ERROR
morning_news             news_agent     0 9 * * *       true     2026-05-16T09:00:00+00:00   -             -             -
weekly_ops_report        ops_agent      0 9 * * MON     true     2026-05-19T09:00:00+00:00   -             -             -
```

**Exit codes:**

| コード | 意味 |
|------|------|
| `0` | 常に（テーブルが空の場合も含む）。 |

## 設定

ジョブは `reyn.yaml` の `cron.jobs` に宣言します。各エントリがスケジュールされたメッセージ配送 1 件に対応します。

```yaml
cron:
  jobs:
    - name: morning_news
      to: news_agent
      message: "今日の主要ニュースをまとめて"
      schedule: "0 9 * * *"
      enabled: true

    - name: weekly_ops_report
      to: ops_agent
      message: "週次運用レポートを生成して"
      schedule: "0 9 * * MON"
      enabled: true
```

| フィールド | 必須 | 説明 |
|-----------|------|------|
| `name` | はい | ジョブの一意な識別子。ログメッセージやステータス照会で使用されます。 |
| `to` | はい | 対象 agent 名。メッセージは `sender="cron:<name>"` 付きでその inbox に配送されます。 |
| `message` | はい | 対象 agent に配送される自由形式テキスト。 |
| `schedule` | はい | 5 フィールドの cron 式（分 時 日 月 曜日）。 |
| `notify` | いいえ | opt-in の unattended 通知チャンネル（例: `"telegram"`）。デフォルトは event-log のみ。 |
| `input` | いいえ | ジョブに付随する追加入力 dict。デフォルトは `{}`。 |
| `enabled` | いいえ | `false` にするとエントリを削除せずに無効化できます。デフォルトは `true`。 |

`to` + `message` を持たない bare な `skill` 名だけのジョブ形は config load 時に `ValueError` で拒否されます — cron ジョブはメッセージベースであり、スキルの直接実行ではありません。

完全なスキーマについては [Reference: `reyn.yaml`](../config/reyn-yaml.md) を参照してください。

## cron 式の構文

式は標準の 5 フィールド形式に従います:

```
┌──────────── 分 (0-59)
│ ┌────────── 時 (0-23)
│ │ ┌──────── 日 (1-31)
│ │ │ ┌────── 月 (1-12 または JAN-DEC)
│ │ │ │ ┌──── 曜日 (0-7 または SUN-SAT; 0 と 7 は日曜日)
│ │ │ │ │
* * * * *
```

よく使う例:

| 式 | 意味 |
|---|------|
| `0 * * * *` | 毎時 0 分 |
| `0 */6 * * *` | 6 時間ごと |
| `0 9 * * MON` | 毎週月曜 09:00 UTC |
| `30 4 1 * *` | 毎月 1 日 04:30 UTC |

時刻はすべて UTC です。スケジューラーは式の解析に [`croniter`](https://pypi.org/project/croniter/) を使用します。

## 関連情報

- [Reference: `reyn.yaml`](../config/reyn-yaml.md) — `cron:` 設定ブロック
- [コンセプト: Operational Intelligence](../../concepts/data-retrieval/operational-intelligence.md) — スケジュール実行のユースケース
- [コンセプト: A2A プロトコル](../../concepts/multi-agent/a2a.md) — `RunRegistry` パターンと将来の Web モードステータス API
- [Reference: `reyn run-once`](run-once.md) — ヘッドレス単発 agent 実行(cron の inbox メッセージ配送とは別の dispatch パス)
