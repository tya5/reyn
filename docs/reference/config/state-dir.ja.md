---
type: reference
topic: config
audience: [human, agent]
applies_to: [.reyn/]
---

# `.reyn/` — 状態ディレクトリ

プロジェクトごとの状態。場所: `<project_root>/.reyn/` — 固定です。（`reyn.yaml` に `state_dir` knob はありません。ランタイムが検出したプロジェクトルートからパスを構築します。）

## レイアウト

```
.reyn/
├── approvals.yaml                          # 永続的な Permission 承認
├── events/                                 # すべてのイベント JSONL ログ
│   ├── direct/                             # `reyn run` からの Skill ラン
│   │   └── skill_runs/<YYYY-MM>/
│   │       └── <ts>_<skill>.jsonl
│   └── agents/<name>/                      # エージェントからの Skill ラン + chat イベント
│       ├── skill_runs/<YYYY-MM>/
│       │   └── <ts>_<skill>.jsonl
│       └── chat/<YYYY-MM>/                 # チャットセッションイベント（サイズ/経過時間でローテーション）
│           └── <ts>.jsonl
├── agents/<name>/                          # エージェントごとの Workspace（エージェントごとに 1 ディレクトリ）
│   ├── profile.yaml                        # エージェント名、ロール、allowed_skills
│   ├── history.jsonl                       # 追記専用の会話ログ
│   ├── memory/                             # エージェントスコープの Memory
│   │   ├── MEMORY.md
│   │   └── <name>.md
│   └── state/                              # WAL Skill ランスナップショット
│       └── skills/<run_id>.snapshot.json
├── skill-versions/<name>/                  # Skill バージョンスナップショット
│   └── v<N>.md
├── eval-results/<skill>/                   # `reyn eval run` の結果ファイル
│   └── <timestamp>.jsonl
├── state/                                  # プロセスグローバルの永続状態
│   └── budget_ledger.jsonl                 # 日次/月次トークン + USD 台帳
└── memory/                                 # プロジェクトスコープの Memory
    ├── MEMORY.md
    └── <name>.md
```

**注意:** `.reyn/config.yaml` は廃止されました。
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

### `events/`

すべてのイベント JSONL ログ。呼び出し元とログタイプで整理されています:

- `direct/skill_runs/<YYYY-MM>/<ts>_<skill>.jsonl` — `reyn run`（非エージェント Skill ラン）からのイベント
- `agents/<name>/skill_runs/<YYYY-MM>/<ts>_<skill>.jsonl` — 名前付きエージェントが生成した Skill ラン イベント
- `agents/<name>/chat/<YYYY-MM>/<ts>.jsonl` — チャットセッションイベント（`events.max_bytes` / `events.max_age_seconds` でローテーション）

JSONL ファイルは `reyn events <file>` でリプレイ可能。[events リファレンス](../runtime/events.md) を参照してください。

### `agents/<name>/`

エージェントごとの Workspace。名前付きエージェントごとに 1 ディレクトリ（`reyn agent new` で作成）。`default` エージェントは常に存在します。

- `profile.yaml` — エージェントのアイデンティティ: 名前、ロール、オプションの `allowed_skills`。[profile-yaml リファレンス](../dsl/profile-yaml.md) を参照。
- `history.jsonl` — 追記専用の会話ログ（ユーザー + アシスタントのターン; クロスエージェントメッセージにはトレース用の `chain_id` が含まれます）。
- `memory/` — エージェントスコープの Memory（`MEMORY.md` インデックス + body ファイル）。ルーターフェーズ中に自動的に検索・書き込み。
- `state/skills/<run_id>.snapshot.json` — 実行中の Skill ランのクラッシュリカバリー用 WAL スナップショット。

### `skill-versions/<name>/`

`skill_improver` が書き込む Skill バージョンスナップショット。各 `v<N>.md` は提案が適用された時点の `skill.md` のタイムスタンプ付きスナップショット。`self_improvement.max_versions` スナップショットまでプルーニングされます。`reyn skill versions <name>` で確認できます。

### `eval-results/<skill>/`

`reyn eval run` 実行ごとに 1 つの JSONL ファイル。各行は 1 つのケース結果を記録: input、expected、実際の `final_output`、スコア、passed フラグ、`skill_version_hash`。`reyn eval report` と `reyn eval compare` で使用されます。

### `state/budget_ledger.jsonl`

永続的な日次・月次トークン + USD 使用量レコード。fsync 付き追記専用。真夜中（日次）または月の 1 日（月次）に自動リセット。`reyn chat` の `/budget` で確認できます。`/budget reset`（インメモリカウンターのみをクリア）の影響を受けません。

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
