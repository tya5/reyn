---
type: how-to
topic: using-reyn
audience: [human]
---

# skill を定期実行する

Reyn は cron スケジュールで skill を自動実行できます — 例えば毎時のイベント
インデックス更新や週次サマリーレポートなど。`reyn.yaml` の `cron.jobs` に
ジョブを宣言し、スケジューラを起動します。

## ジョブを宣言する

各ジョブには skill 名・5 フィールドの cron 式・任意の入力を指定します:

```yaml
# reyn.yaml
cron:
  jobs:
    - name: weekly_ops_report
      skill: ops_report
      schedule: "0 9 * * MON"   # 毎週月曜 09:00
      input:
        since_days: 7
      enabled: true
```

| フィールド | 必須 | 意味 |
|-----------|------|------|
| `name` | はい | 一意なジョブ識別子 |
| `skill` | はい | 実行する stdlib またはプロジェクト skill |
| `schedule` | はい | 5 フィールド cron 式（分 / 時 / 日 / 月 / 曜日） |
| `input` | いいえ（既定 `{}`） | skill に渡す入力アーティファクト |
| `enabled` | いいえ（既定 `true`） | `false` でエントリは残しつつスケジュール対象外に |

## スケジューラを起動する

```bash
reyn cron run
```

これは**フォアグラウンド**で動作し、Ctrl-C を押すまでブロックします。起動時に
各有効ジョブの次回発火時刻バナーを表示し、`reyn run` と同じヘッドレス経路で
各ジョブをスケジュール時刻に dispatch します。

```
$ reyn cron run
Started cron scheduler with 1 enabled job(s):
  • weekly_ops_report  (0 9 * * MON)  next: 2026-05-19T09:00:00+00:00
^C
Cron scheduler stopped.
```

スケジューラは `reyn web` 内（FastAPI lifespan）でも自動起動するため、web
サーバーを動かしていれば別途 `reyn cron run` プロセスなしでジョブが発火し続けます。

## 起動せずにジョブを確認する

```bash
reyn cron list      # 全ジョブ + 次回発火時刻
reyn cron status    # 最終実行状態
```

`reyn cron list` は `cron.jobs` を読み、スケジューラを起動せずにテーブルを表示します。

## 補足

- **`reyn cron status` は稼働中スケジューラのみ反映**します。v1 では最終実行状態が
  メモリ内のため、`reyn cron run` セッション外では表示できる実行履歴がありません。
- ジョブは**ヘッドレス**で実行され、対話プロンプトはありません。skill が必要とする
  権限は `reyn.yaml` で事前承認しておいてください（スケジュール実行は確認のために
  停止できません）。
- 実行時登録ジョブは `<project>/.reyn/cron.yaml` に保存され、名前衝突時は
  `reyn.yaml` の `cron.jobs` を上書きします。

## 関連

- [リファレンス: `reyn cron`](../../reference/cli/cron.md) — `run` / `list` / `status`、出力形式、終了コード
- [リファレンス: `reyn.yaml` — `cron:` ブロック](../../reference/config/reyn-yaml.md#cron-block) — 全フィールドスキーマ
- [支出に上限をかける](cap-spending.md) — スケジュールジョブも日次/月次予算にカウントされます
