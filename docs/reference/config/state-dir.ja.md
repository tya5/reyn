---
type: reference
topic: config
audience: [human, agent]
applies_to: [.reyn/]
---

# `.reyn/` — 状態ディレクトリ

プロジェクトごとの状態。デフォルトの場所: `<project_root>/.reyn/`。`reyn.yaml` の `state_dir` キーでオーバーライドします。

## レイアウト

```
.reyn/
├── config.yaml          # 個人設定のオーバーライド（通常 gitignored）
├── approvals.yaml       # 永続的な Permission 承認
├── events/              # イベント JSONL ログ、ランごとに 1 ファイル
│   └── <run_id>.jsonl
├── chats/               # chat セッション状態（セッションごとに 1 ファイル）
│   └── <session_id>.json
└── memory/              # プロジェクトスコープの Memory
    ├── MEMORY.md
    └── <name>.md
```

### `config.yaml`

`reyn.yaml` の個人設定オーバーライド。同じスキーマ。通常 gitignored。`api_base`、カスタム `models` などに使用します。

### `approvals.yaml`

インタラクティブなプロンプトからの永続的な Permission 承認。`<skill>/<op>/<path>` をキーとします。[permissions.md](permissions.md) を参照してください。

```yaml
my_skill/file.write//tmp/output: just_path
my_skill/shell: allow
```

`reyn permissions list` で確認します。`reyn permissions revoke <key>` で削除します。

### `events/<run_id>.jsonl`

ランの実行中に発行されたすべてのイベントの JSONL ログ。`reyn events <file>` でリプレイ可能。[events リファレンス](../runtime/events.md) を参照してください。

### `chats/<session_id>.json`

`reyn chat` セッションの状態: 履歴、永続化された Memory 検索結果など。

### `memory/`

プロジェクトスコープの Memory — ランをまたいで永続化すべき、かつプロジェクト固有のファクト。グローバル Memory は代わりに `~/.reyn/memory/` に格納されます。

`MEMORY.md` はインデックスです。各 `<name>.md` は frontmatter（`type`、`name`、`description`）を持つ 1 つの Memory エントリーです。

## グローバル状態（`~/.reyn/`）

`.reyn/` と同じ形状ですが、ホームディレクトリに格納されます。以下に使用されます:

- `~/.reyn/config.yaml` — ユーザーグローバルのデフォルト。
- `~/.reyn/memory/` — グローバル Memory（プロジェクトに紐づかないユーザーに関するファクト）。

`recall_memory` と `write_memory` はグローバルとプロジェクトの両スコープを参照します。

## Gitignore

推奨追加内容:

```
.reyn/config.yaml
.reyn/events/
.reyn/chats/
.reyn/approvals.yaml
```

Memory（`.reyn/memory/`）— プロジェクト Memory がコラボレーター間で共有されるかどうかに基づいて選択します。

## 関連情報

- [reyn-yaml.md](reyn-yaml.md) — `state_dir` 設定
- [permissions.md](permissions.md) — approvals.yaml の詳細
- [リファレンス: events](../runtime/events.md)
