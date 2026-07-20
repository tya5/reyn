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
│   ├── profile.yaml                        # エージェント名、ロール、allowed_mcp
│   ├── history.jsonl                       # 追記専用の会話ログ
│   ├── memory/                             # エージェントスコープの Memory
│   │   ├── MEMORY.md
│   │   └── <name>.md
│   └── state/                              # WAL Skill ランスナップショット
│       └── skills/<run_id>.snapshot.json
├── skill-versions/<name>/                  # Skill バージョンスナップショット
│   └── v<N>.md
├── state/                                  # プロセスグローバルの永続状態
│   ├── budget_ledger.jsonl                 # 耐久 budget 台帳（日次/月次/per-agent）
│   └── budget_state.json                   # 台帳上の throttle 付きベストエフォートキャッシュ
├── cache/
│   └── budget_checkpoint.json              # 圧縮済み per-agent チェックポイント（#2945、削除して安全）
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

- `profile.yaml` — エージェントのアイデンティティ: 名前、ロール、オプションの `allowed_mcp`。profile-yaml リファレンス を参照。
- `history.jsonl` — 追記専用の会話ログ（ユーザー + アシスタントのターン; クロスエージェントメッセージにはトレース用の `chain_id` が含まれます）。
- `memory/` — エージェントスコープの Memory（`MEMORY.md` インデックス + body ファイル）。ルーターフェーズ中に自動的に検索・書き込み。
- `state/skills/<run_id>.snapshot.json` — 実行中の Skill ランのクラッシュリカバリー用 WAL スナップショット。

### `skill-versions/<name>/`

`skill_improver` が書き込む Skill バージョンスナップショット。各 `v<N>.md` は提案が適用された時点の `skill.md` のタイムスタンプ付きスナップショット。`self_improvement.max_versions` スナップショットまでプルーニングされます。`reyn skill versions <name>` で確認できます。

### `state/budget_ledger.jsonl`

耐久的な追記専用 budget レコードログ（追記ごとに fsync）。1 LLM 呼び出し = 1 レコード（token + USD 使用量）を保持します。過去の ledger には per-chain skill spawn cap 削除前に書かれたレガシーレコード（`kind: "spawn"`）が残っている場合がありますが、現在は書き込まれず読み込み時にスキップされます。起動時に Reyn は日次・月次合計（真夜中／月初に自動リセット）と累積 per-agent token + USD 合計を再集計します。これにより、すべての budget cap がプロセス再起動・クラッシュをまたいで保持されます。これが cap の信頼源（source of truth）です。`reyn chat` の `/budget` で確認できます。`/budget reset`（インメモリカウンターのみをクリア）の影響を受けません。

ledger はローテーションされないため、`hydrate` は毎回全体を再パースするわけではありません（#2945）。圧縮済みの per-agent チェックポイント（下記 `cache/budget_checkpoint.json`）を読み、そのアンカー以降に追記された tail だけを再パースします。チェックポイントが欠損・破損している場合は全体再スキャンにフォールバックします。ledger がチェックポイントの anchor より前まで truncate されている場合（削除された場合を含む）は、チェックポイントの per-agent 合計をその再スキャン結果に **floor（下限）** としてマージします — 黙って捨てることはしません。これにより、truncate/消失した ledger が cap-critical な per-agent 合計を under-count することはありません。ledger が**同サイズ以上の別内容に置き換えられた**場合（縮んではいないが内容が一致しない）は floor を適用せず、新しい ledger の全体再スキャンのみを信頼します。

### `state/budget_state.json`

インメモリ budget カウンターの throttle 付きベストエフォートなスナップショット。短い間隔で台帳上のキャッシュとして書き込まれます。台帳に対して最大 1 秒遅れることがあるため、復旧時は台帳の値が常に優先されます。削除しても安全で、台帳が正本です。

### `cache/budget_checkpoint.json`

`budget_ledger.jsonl` の per-agent 生涯合計を、ledger 上の正確なバイト位置にアンカーして圧縮したサマリ（#2945）。`budget_state.json` と同様に自動更新されます。書き込みに失敗しても（read-only ディレクトリ、ディスクフル等）ログに記録して握りつぶすだけで、起動を妨げません — DERIVED/cache であり ledger から常に再構築できるためです。

正しさの観点では削除して安全です（`hydrate` が ledger から再構築します。全体再スキャンのコストはかかります）。ただし「per-agent cap のリセット」と同義では**ありません**: このチェックポイントが残ったまま ledger だけを削除・アーカイブしても per-agent 合計はリセットされません — floor として生き残ります（上記 `state/budget_ledger.jsonl` 参照）。per-agent の spend を本当にリセットするには、プロセス停止中に両方のファイルを一緒にアーカイブしてください。

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
