---
type: how-to
topic: config
audience: [human]
applies_to: [.reyn/, reyn.yaml]
---

# 永続化された状態を管理する

**目的:** コミットすべき Reyn の状態、gitignore にすべきもの、格納場所を決定する。

## `.reyn/` 配下に格納されるもの

`.reyn/` は **opaque な runtime 状態ディレクトリ** です — ツール管理と見なし、人間が直接編集しないようにしてください。

| パス | 目的 | デフォルトの git ステータス |
|------|---------|--------------------|
| `.reyn/approvals.yaml` | 保存された Permission 承認 | gitignore |
| `.reyn/events/` | ランごとのイベント JSONL ログ | gitignore |
| `.reyn/agents/` | agent ごとのプロファイル・チャット履歴・状態 | gitignore |
| `.reyn/eval-results/` | Skill ごとの eval 結果 | gitignore |
| `.reyn/memory/` | プロジェクトスコープの Memory | チームによる |
| `.reyn/state/` | WAL + バジェット台帳（クラッシュリカバリー） | gitignore |

`reyn.yaml`（プロジェクト設定）はチェックインします。個人設定のオーバーライドは
`reyn.local.yaml`（gitignored、プロジェクトルート）に置いてください — `.reyn/config.yaml` ではありません。

**注意:** `.reyn/config.yaml` は廃止されました（ADR-0031）。存在する場合、Reyn は警告を表示して無視します。
内容を `reyn.local.yaml` に移行してください。

## 推奨される `.gitignore`

```
.reyn/
reyn.local.yaml
```

Memory は判断が必要です:

- **`.reyn/memory/` をコミットする** — プロジェクト Memory が共有知識（規約、決定事項）であり、コラボレーターが恩恵を受ける場合。
- **gitignore にする** — Memory が push したくない開発者個人のメモの場合。

## 状態を別の場所に移動する

デフォルトの場所は `<project_root>/.reyn/` です。プロジェクトごとにオーバーライドします:

```yaml
# reyn.yaml
state_dir: /var/lib/reyn/<project>
```

または `--state-dir` でランごとに（サブコマンドがサポートする場合）— 通常はプロジェクト設定で十分です。

## グローバル状態

`~/.reyn/` はプロジェクトごとの形状を反映します:

- `~/.reyn/config.yaml` — ユーザーグローバルのデフォルト（デフォルトモデル、API ベースなど）。このファイルは引き続き有効です（廃止されていません）。
- `~/.reyn/memory/` — グローバル Memory（すべてのプロジェクトにまたがるユーザーに関するファクト）。

`recall_memory` はグローバルとプロジェクトの両方のスコープを読み取ります。

## 削除しても安全なもの

| パス | 削除しても安全? | 備考 |
|------|-----------------|-------|
| `.reyn/events/` | Yes | ただのログ。リプレイデータが失われます。 |
| `.reyn/eval-results/` | Yes | 再生成可能。 |
| `.reyn/agents/` | Yes | agent プロファイル・チャット履歴・スキル再開チェックポイントが失われます。 |
| `.reyn/approvals.yaml` | Yes | 次回の実行時に再プロンプトされます。 |
| `.reyn/memory/` | 場合による | 永続化されたファクトが失われます。先にエクスポートします: `reyn memory export --out memory.json`。 |

`reyn.yaml` と `reyn.local.yaml` は設定です。削除するとデフォルトにリセットされます。

## 関連情報

- [リファレンス: state-dir](../../reference/config/state-dir.md)
- [リファレンス: reyn.yaml](../../reference/config/reyn-yaml.md) — `state_dir` キー
- [コンセプト: memory](../../concepts/memory.md)
