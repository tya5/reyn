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
├── approvals.yaml       # 永続的な Permission 承認
├── events/              # イベント JSONL ログ、ランごとに 1 ファイル
│   └── <run_id>.jsonl
├── chats/               # chat セッション状態（セッションごとに 1 ファイル）
│   └── <session_id>.json
├── state/               # WAL とバジェット台帳（クラッシュリカバリー）
└── memory/              # プロジェクトスコープの Memory
    ├── MEMORY.md
    └── <name>.md
```

**注意:** `.reyn/config.yaml` は ADR-0031（3-layer config cascade）で廃止されました。
個人設定のオーバーライドは `reyn.local.yaml`（gitignored、プロジェクトルート）に置いてください。
既存の `.reyn/config.yaml` がある場合は、内容を `reyn.local.yaml` に移行して旧ファイルを削除してください。
削除するまで Reyn は警告を表示します。

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
.reyn/
reyn.local.yaml
```

Memory（`.reyn/memory/`）— プロジェクト Memory がコラボレーター間で共有されるかどうかに基づいて選択します。

## 関連情報

- [reyn-yaml.md](reyn-yaml.md) — `state_dir` 設定
- [permissions.md](permissions.md) — approvals.yaml の詳細
- [リファレンス: events](../runtime/events.md)
