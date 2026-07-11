---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn chat]
---

# `reyn chat`

agent にアタッチされたインタラクティブな REPL セッションを開始します。各ユーザーターンは `skill_router` stdlib Skill を通じてディスパッチされ、意図を分類して直接返信、プロジェクト/stdlib Skill の実行、または別の agent への委任を行います。

Memory の検索と書き込みはルーター Phase の内部で自動的に行われます。[コンセプト/memory](../../concepts/data-retrieval/memory.md) を参照してください。

## 概要

```
reyn chat [agent_name] [OPTIONS]
```

`agent_name` は位置引数でオプションです。省略すると、Reyn は自動作成された `default` agent にアタッチします。

## オプション

共通ランタイムフラグ（`--model`、`--output-language`、`--phase-budget`、`--llm-timeout`、`--llm-max-retries`）は `reyn run-once` と共有です。[共通フラグ](common-flags.md) を参照してください。

chat 固有のフラグ:

| フラグ | デフォルト | 説明 |
|------|---------|-------------|
| `--cui` | オフ | プレーンコンソール出力（TUI なし）を使用。パイプ、デバッグ、ヘッドレス環境に便利。 |
| `--no-restore` | オフ | 起動時にディスクからの進行中 Skill ステートの復元をスキップ。デバッグやクリーンなセッション開始に便利。 |
| `--reset` | オフ | 起動前に進行中 Skill ステート（スナップショット + WAL）を消去。`.reyn/events/` の監査ログは保持されます。 |
| `--banner` | オフ | ASCII アートの起動バナー（グラデーション REYN ロゴ + agent/モデル情報）を表示。 |
| `--eager-embedding-build` | オフ | 初回ターンでアクション埋め込みインデックスのビルドを同期的に待機(1 回のみ約 2〜5 秒)。`search_actions` を即時利用可能にする。 |
| `--grant-file-write` | オフ | このセッションのリゾルバ層で `file.read`/`file.write` を許可(サンドボックスの書き込みゾーンにスコープ)。非対話/スクリプト用途向け — agent がワーキングツリーを編集する必要があると分かっていて、Skill ごとの許可プロンプトを避けたい場合に。 |
| `--exclude-tools NAMES` | — | agent の LLM 可視カタログから隠すツール名(カンマ区切り、例 `web__search,web__fetch`)。ツール自体は存在し、agent が名前で呼び出せば実行されるが、このセッションではモデルの発見サーフェスには表示されない。 |
| `--connect <URL>` | オフ | ローカルセッションを起動する代わりに、リモートの `reyn web` サーバーへ AG-UI(HTTP+SSE)経由でアタッチする(例: `--connect http://127.0.0.1:8080`)。位置引数 `agent_name` でサーバー上の agent を選択。`pip install reyn[web]` が必要。TTY であっても常にプレーンコンソールセッションとして描画される(`--cui` と同じ出力スタイル)— インラインステータスバーやスラッシュ補完メニューは無し。`/rewind` はインタラクティブピッカーでなくプレーンなテキストリストにフォールバック。[How-to: リモートシンクライアント](../../guide/for-users/chat-and-web-ui.ja.md#reyn-chat-connect) 参照。 |
| `--token <SECRET>` | オフ | `--connect` 用のベアラートークン(`reyn web` が起動時に表示するシークレット、または `web.auth.token` で設定したトークン)。`REYN_WEB_AUTH_TOKEN` 環境変数にフォールバック。同一マシンの UDS サーバーではトークン不要な場合がある。 |

## agent Workspace

各 agent は `.reyn/agents/<name>/` 配下に状態を永続化します:

- `profile.yaml` — 名前、ロール、オプションの `allowed_mcp`(リファレンス)
- `history.jsonl` — 追記専用の会話ログ（chat + agent 間メッセージ、クロス agent トレース用の chain_id 付き）
- `events.jsonl` — `reyn events` 用のランタイムイベント
- `memory/` — agent スコープの Memory レイヤー（`MEMORY.md` + body ファイル）
- `runs/` — 起動された Skill のランの Workspace

前の会話を再開するには、同じ agent にアタッチします:

```bash
reyn chat researcher
```

`default` agent は常に存在します。[`reyn agent new`](agent.md) でさらに作成します。

## スラッシュコマンド

セッションがアクティブな間、`/` で始まる行は処理され、agent にルーティングされません。

| コマンド | 効果 |
|---------|--------|
| `/list` | 実行中の Skill スポーンと保留中の介入を表示 |
| `/cancel <id>` | Skill スポーンをキャンセル（完全な id または最後の 4 文字） |
| `/answer <id> <text>` | 保留中の `ask_user` / Permission プロンプトに回答 |
| `/agents` | 読み込まれた agent と現在アタッチされているものを一覧表示 |
| `/attach <name>` | REPL ポインターを別の agent に切り替える（前の agent はバックグラウンドで実行し続ける） |
| `/session new \| switch <sid> \| list` | アタッチ中 agent の会話セッションを開く / 切り替える / 一覧表示（[Sessions](../../concepts/multi-agent/sessions.md) 参照） |
| `/skill list` | 実行中の Skill 実行を表示（id / 名前 / current_phase + 親子関係） |
| `/skill discard <run_id>` | 特定の Skill 実行を中止して cleanup を実行 |
| `/tasks` | 動作中の Skill 実行の統合ビュー。`/tasks list` と同じ |
| `/tasks status <prefix>` | 特定の Skill 実行の current phase + 経過時間を表示 |
| `/tasks kill <prefix>` | 特定の Skill 実行を中止。prefix は Skill の run_id とマッチ |

`/list` / `/cancel` / `/answer` は基盤となります。複数の Skill 実行と介入がプロンプトをブロックせずに共存できます。`/agents` / `/attach` はマルチエージェントワークフローのプリミティブです。`/skill` は crash recovery オペレーターコマンドで、per-skill-run のライフサイクルを surface します。`/tasks` は Skill 実行の統合エントリーポイントで、Skill が spawn された後に LLM が user に `/tasks` で進捗確認を案内します。

## マルチエージェントの動作

ルーターがこのターンは別の agent が処理した方がよいと判断した場合、`skills_to_run` エントリーの代わりに（またはそれに加えて）`messages_to_agents` エントリーを出力します。受信 agent はリクエストを非同期に処理します。返信は発信元のチェーンに自動ルーティングされて戻ります。完全なモデルについては [コンセプト/multi-agent](../../concepts/multi-agent/multi-agent.md) を参照してください。

ユーザーが開始したチェーンは中間の `reply_text`（発信元 agent の最初のルーターターン）を発行し、その後デリゲートのレスポンスが届いた後に最終的な統合された返信が続きます。これにより、ホップをまたいでも「作業中です」という UX が保たれます。

`/attach` スラッシュコマンドでチェーン途中のデリゲートの進捗を監視できます。前の agent の `session.run()` は受信トレイを消費し続けるので、後で戻っても問題なく解決されます。

## Permission の動作

`reyn chat` はインタラクティブです。サブ Skill がデフォルト外の Permission を必要とする場合、介入キュー経由で応答するまでプロンプトがブロックします。選択は `.reyn/approvals.yaml` に永続化できます（[permissions リファレンス](../config/permissions.md) を参照）。

## 例

デフォルト agent に対して新しいセッションを開始:

```bash
reyn chat
```

名前付き agent にアタッチ:

```bash
reyn chat researcher
```

この会話のみにより強力なモデルを使用:

```bash
reyn chat --model strong
```

## 関連情報

- [リファレンス: agent CLI](agent.md) — `reyn agent new / list / show / rm`
- [リファレンス: topology CLI](topology.md) — `reyn topology` で通信構造を宣言
- リファレンス: profile-yaml
- [リファレンス: multi-agent 設定](../config/multi-agent.md) — `safety.loop.max_agent_hops`
- [リファレンス: state-dir](../config/state-dir.md) — `agents/` の場所
- [コンセプト: multi-agent](../../concepts/multi-agent/multi-agent.md)
- [コンセプト: memory](../../concepts/data-retrieval/memory.md)

