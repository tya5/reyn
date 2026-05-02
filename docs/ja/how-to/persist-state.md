---
type: how-to
topic: config
audience: [human]
applies_to: [.reyn/, reyn.yaml]
---

# 永続化された状態を管理する

**目的:** コミットすべき Reyn の状態、gitignore にすべきもの、格納場所を決定する。

## `.reyn/` 配下に格納されるもの

| パス | 目的 | デフォルトの git ステータス |
|------|---------|--------------------|
| `.reyn/config.yaml` | `reyn.yaml` の個人設定オーバーライド | gitignore |
| `.reyn/approvals.yaml` | 保存された Permission 承認 | gitignore |
| `.reyn/events/` | ランごとのイベント JSONL ログ | gitignore |
| `.reyn/chats/` | chat セッション履歴 | gitignore |
| `.reyn/eval_reports/` | Skill ごとの eval 結果 | gitignore |
| `.reyn/memory/` | プロジェクトスコープの Memory | チームによる |

`reyn.yaml`（プロジェクト設定）はチェックインします。`.reyn/config.yaml`（個人設定）はチェックインしません。

## 推奨される `.gitignore`

```
.reyn/config.yaml
.reyn/approvals.yaml
.reyn/events/
.reyn/chats/
.reyn/eval_reports/
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

- `~/.reyn/config.yaml` — ユーザーグローバルのデフォルト（デフォルトモデル、API ベースなど）。
- `~/.reyn/memory/` — グローバル Memory（すべてのプロジェクトにまたがるユーザーに関するファクト）。

`recall_memory` はグローバルとプロジェクトの両方のスコープを読み取ります。

## 削除しても安全なもの

| パス | 削除しても安全? | 備考 |
|------|-----------------|-------|
| `.reyn/events/` | Yes | ただのログ。リプレイデータが失われます。 |
| `.reyn/eval_reports/` | Yes | 再生成可能。 |
| `.reyn/chats/` | Yes | セッションを再開する能力が失われます。 |
| `.reyn/approvals.yaml` | Yes | 次回の実行時に再プロンプトされます。 |
| `.reyn/memory/` | 場合による | 永続化されたファクトが失われます。先にエクスポートします: `reyn memory export --out memory.json`。 |

`reyn.yaml` と `.reyn/config.yaml` は設定です。削除するとデフォルトにリセットされます。

## 関連情報

- [リファレンス: state-dir](../reference/config/state-dir.md)
- [リファレンス: reyn.yaml](../reference/config/reyn-yaml.md) — `state_dir` キー
- [コンセプト: memory](../concepts/memory.md)
