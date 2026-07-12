---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn chat]
---

# `reyn chat`

agent にアタッチされたインタラクティブな REPL セッションを開始します。各ユーザーターンは chat router を通じてディスパッチされ、意図を分類して直接返信、Skill の実行、または別の agent への委任を行います。

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
| `/agent edit role <text>` | アタッチ中 agent のペルソナを書き換え |
| `/agent new <name>` | 新規 agent を作成してアタッチ |
| `/agents` | 読み込まれた agent と現在アタッチされているものを一覧表示 |
| `/answer <id-prefix> <text>` | 保留中の `ask_user` / Permission プロンプトに回答(id-prefix: intervention id の任意の一意な prefix) |
| `/attach <name>` | REPL ポインターを別の agent に切り替える(前の agent はバックグラウンドで実行し続ける) |
| `/budget [reset]` | 完全な budget 内訳; `/budget reset` はプロセス単位のカウンタをクリア([config/budget](../config/budget.md) 参照) |
| `/clear-history`(alias `/clear`) | チャット履歴を消去(**破壊的**; メモリ内 + 永続履歴 + action-usage テーブルをクリア。events/run-state/profile は保持) |
| `/compact` | コンテキストウィンドウを空けるため今すぐ会話履歴を圧縮する([chat-compaction](../../concepts/data-retrieval/chat-compaction.md) 参照) |
| `/concept <term>` | 用語集のインライン参照 |
| `/copy [N\|list]` | agent の返信をクリップボードにコピー(1 = 最新、2 = 1 ターン前、…) |
| `/cost` | このagentのトークン + USD コスト概要 |
| `/exit` | チャットを終了(alias: `/quit`、Ctrl+D) |
| `/help [<cmd>]` | スラッシュコマンドのヘルプ — 全件一覧、または特定コマンドに絞る |
| `/hook on\|off <name>` | このセッションでの hook の有効/無効を切り替え(次回 dispatch から有効; セッションスコープ — agent の他セッションでは引き続き発火。再起動後も持続) |
| `/image <path>`(alias `/img`) | 次のユーザーメッセージに画像を添付(マルチモーダル入力; png/jpg/jpeg/gif/webp/svg) |
| `/list` | 保留中の介入を一覧表示 |
| `/memory [list\|view <name>]` | プロジェクトメモリのエントリを確認([concepts/memory](../../concepts/data-retrieval/memory.md) 参照) |
| `/model [<class>]` | セッションのモデルクラスとオーバーライドを表示、または `/model <class>` でセッション単位のモデルクラスオーバーライドを設定(既知クラスに対して validate; 再起動でクリア) |
| `/pending [list\|discard <id>\|claim <id>]` | 停滞したクロスチャネル操作を一覧表示 / 破棄 / claim |
| `/quit` | チャットを終了(alias: `/exit`、Ctrl+D) |
| `/reload` | 次のターン境界でランタイム設定(`.reyn/*.yaml`)をホットリロード |
| `/reset confirm` | 進行中の run 状態をリセット(スナップショット + WAL; 監査ログは保持) |
| `/rewind [seq]` | 以前のチェックポイントへタイムトラベル — 引数無しでピッカーメニューを開く、`seq` で直接ジャンプ([Time-travel](../../concepts/runtime/time-travel.md) · [How-to](../../guide/for-users/time-travel.md) 参照) |
| `/session new \| switch <sid> \| list` | アタッチ中 agent の会話セッションを開く / 切り替える / 一覧表示([Sessions](../../concepts/multi-agent/sessions.md) 参照) |
| `/tasks [list]` | LLM が `task__create` で作成した dynamic task を一覧表示 |
| `/tasks status <task_id-prefix>` | 特定の task のステータス + 依存関係を表示 |
| `/tasks kill <task_id-prefix>` | 特定の dynamic task を中止 |
| `/visibility on\|off <tool\|mcp\|category> <name>` | このセッションでの capability の LLM 可視性を切り替え(次ターンで非表示 / agent の許可済 envelope の範囲内で復元 — envelope が拒否する capability は非表示のまま) |

`/list` / `/answer` は基盤となります。保留中の介入がプロンプトをブロックせずに共存できます。`/agents` / `/attach` / `/agent` はマルチエージェントワークフローのプリミティブです。`/tasks` は LLM が `task__create` で spawn する dynamic task のエントリーポイントです — 実行中のものを一覧表示、特定タスクのステータス/依存関係を確認、または kill します; task 作成後 LLM も user に `/tasks` を案内します。`/hook` / `/visibility` はセッションスコープの LLM カタログ制御で、ステータスバーの `hook`/`tool`/`mcp`/`category` チップと対応します。`/copy` は会話ペインのユーティリティ; `/image` はマルチモーダル入力を可能にします。

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

