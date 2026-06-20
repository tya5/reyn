# 人気のローカル MCP サーバー — インストール + 利用手順

Reyn で人気のローカル MCP サーバー 5 種を、コピー&ペーストで実行できる手順としてまとめました。各セクションは以下を示します:

1. **Install** — 1 つの `reyn mcp install` コマンド。手動編集なしでローダーが読み込める設定を生成します。
2. **Direct smoke** — `scripts/mcp_smoke.py` ランナー経由での接続性 / ツール探索 / 1 ツール呼び出し。「サーバーが生きているか」の確認に便利です。
3. **Usage from chat** — チャットルーター経由でサーバーを動かす実際の `reyn chat` 会話。ルーターはカタログが部分的であることを通知するため、LLM はホットリストに見えない機能を発見しようと能動的に `list_actions`（キーワード / セマンティッククエリでは `search_actions`）を呼びます。

対象サーバー:

- [time](#time) — タイムゾーン対応の現在時刻 / 変換
- [git](#git) — ローカルリポジトリ操作（log / status / diff / branch）
- [sequential-thinking](#sequential-thinking) — chain-of-thought スクラッチパッド
- [sqlite](#sqlite) — ローカル DB クエリ（読み取り + 書き込み + スキーマ）
- [everything](#everything) — プロトコルのプリミティブを一通り網羅するデモ

> **なぜ filesystem / memory / fetch のセクションが無いのか?** これら 3 つの MCP サーバーは、構造的に Reyn の組み込み op と重複します:
>
> - filesystem ↔ `file__*`（= read / write / list / grep / glob）
> - memory ↔ `memory_operation__*`（= remember_shared / forget）
> - fetch ↔ `web__fetch`（= markdown 抽出付きの HTTP fetch）
>
> 両方が利用可能な場合、チャットルーターは自然なプロンプトに対して一貫して Reyn 内部の op を選びます（測定で 10/10、2026-05-21）。MCP サーバーはエージェント経路では動かされません。
>
> **抽出パリティ**: `pip install reyn[fetch]` をインストールすると、trafilatura が `web__fetch` の HTML 抽出器として追加されます。その時点で、Reyn の op はコンテンツの濃いページにおいて `mcp-server-fetch` の抽出品質に匹敵します。MCP サーバーに残る優位性は `start_index` ページネーションと robots.txt 認識です。それらが特に必要な場合のみ、直接呼び出し（`scripts/mcp_smoke.py`）か MCP サーバー自体を使ってください。

> **チャット履歴の汚染に関する注意。** エージェントが以前ある機能を拒否した場合（= LLM が「私には…できません」と言った）、SP のシグナルが別を指示していても、in-context learning が後続ターンで拒否パターンを継続することがあります。下記の利用例が期待するツール呼び出しを生成しない場合は、まずエージェントの履歴をクリアしてください:
>
> ```bash
> echo -n > .reyn/agents/default/history.jsonl
> ```
>
> 下記の自然プロンプト利用例のクリーン状態（履歴新規）での成功率は約 90% です（2026-05-21 に `gemini-2.5-flash-lite` で測定）。汚染履歴では成功率が急激に低下します。セッションをまたいで問題が続く場合は、エージェントのシステムプロンプトがそのツールを正しく公開しているか確認してください。

## 前提条件

- `node` + `npx`（ほとんどのサーバーは npm パッケージ）
- `uv` + `uvx`（= Python サーバー; `brew install uv`）
- `[mcp]` extra 付きでインストールされた Reyn: `pip install -e ".[mcp]"`
- チャット利用の場合: サーバーごとに 1 度だけ per-server permission を事前承認（= 各セクションに示す 1 行）。

smoke ランナー `scripts/mcp_smoke.py` はチャットルーターをバイパスして直接 `reyn.mcp.client.MCPClient` に向かいます。接続性の健全性確認に便利です。エージェント駆動の利用（= 典型的なエンドユーザーの形）では、チャットルーターが汎用の `mcp__call_tool` / `invoke_action` ディスパッチ経由でサーバーを呼びます。

> 各セクションの `reyn mcp install --source ...` シェルコマンドには、チャット側の同等な動詞があります: `mcp__install_package({kind, identifier, version?})`（= 同じパッケージチャネル: `npm` / `pypi` / `docker` / `github`）。ワークフローに合う面を使ってください。どちらも同じ `.reyn/mcp.yaml` への書き込みに収束します。完全な対応は [`reyn mcp` CLI § Chat-side equivalents](../../reference/cli/mcp.md#chat-side-equivalents) を参照してください。

---

## time

タイムゾーン対応の時刻クエリ。Python ベース（uvx）なので `uv` が必要です。

### 前提条件

```bash
brew install uv
```

### Install

```bash
reyn mcp install --source pypi:mcp-server-time --non-interactive
```

### Direct smoke

```bash
python scripts/mcp_smoke.py time get_current_time '{"timezone": "Asia/Tokyo"}'
```

期待値: `content[0].text` が `{"timezone": "Asia/Tokyo", "datetime": "<ISO 8601>", ...}` を含む。

### Usage from chat

```bash
echo 'mcp.time: true' >> .reyn/approvals.yaml

reyn chat
> What time is it in Tokyo right now?
```

エージェントは `mcp__call_tool({tool: "time__get_current_time", tool_args: {timezone: "Asia/Tokyo"}})` を呼び、自然言語で応答します。複数タイムゾーンのクエリ（「Tokyo, NYC, London」）では、エージェントは 3 回の呼び出しを連鎖させ、1 つの回答に統合します。

### 公開されるツール

`get_current_time` / `convert_time`。

---

## git

`mcp-server-git`（Python / uvx）経由のローカル git リポジトリ操作。

### Install

```bash
reyn mcp install --source pypi:mcp-server-git --non-interactive
```

### Direct smoke

```bash
python scripts/mcp_smoke.py git git_log "{\"repo_path\": \"$PWD\", \"max_count\": 3}"
python scripts/mcp_smoke.py git git_branch "{\"repo_path\": \"$PWD\", \"branch_type\": \"local\"}"
```

期待値: 直近 3 件のコミット / ローカルブランチが列挙される。

### Usage from chat

```bash
echo 'mcp.git: true' >> .reyn/approvals.yaml

reyn chat
> Summarise the last 3 commits in this repo.
```

エージェントは `mcp__call_tool({tool: "git__git_log", tool_args: {repo_path: "<session cwd>", max_count: 3}})` を呼び、短い要約を生成します。

### 公開されるツール

`git_status` / `git_diff_unstaged` / `git_diff_staged` / `git_diff` /
`git_commit` / `git_add` / `git_reset` / `git_log` /
`git_create_branch` / `git_checkout` / `git_show` / `git_branch`。

---

## sequential-thinking

ガイド付きの chain-of-thought 推論のためのメタツール。I/O ではなく *ワークフローパターン* をラップする MCP サーバーのデモとして有用です。

### Install

```bash
reyn mcp install --source npm:@modelcontextprotocol/server-sequential-thinking --non-interactive
```

### Direct smoke

```bash
python scripts/mcp_smoke.py sequential-thinking sequentialthinking '{
  "thought": "Verify the smoke harness works for stateful tools.",
  "thoughtNumber": 1,
  "totalThoughts": 1,
  "nextThoughtNeeded": false
}'
```

期待値: `structuredContent` が `{"thoughtNumber": 1, ...}` を含む。

### Usage from chat

```bash
echo 'mcp.sequential-thinking: true' >> .reyn/approvals.yaml

reyn chat
> Use sequential-thinking to plan how to organise a personal task list.
```

エージェントは一連の `mcp__call_tool({tool: "sequential_thinking__sequentialthinking", args: {...}})` 呼び出し（通常 5〜7 thoughts）を発行し、その連鎖を自然言語のプランに統合します。サーバーは thought の履歴を内部で追跡し、複数の呼び出しが 1 つのサーバー存続期間内で連鎖を積み上げます。

> 注: ユーザープロンプト内のキーワード「sequential-thinking」は、汎用の問題解決経路（= 明確なターゲットの無い invoke_action）ではなくこのサーバーをルーターが選ぶのを助けます。

### 公開されるツール

`sequentialthinking`（単一ツール — 連鎖呼び出しが thought シーケンスを積み上げる）。

---

## sqlite

`mcp-server-sqlite`（Python / uvx）経由のローカル SQLite データベース。

### Install

```bash
mkdir -p ./.mcp-sandbox && rm -f ./.mcp-sandbox/test.db

reyn mcp install --source pypi:mcp-server-sqlite \
    --args "--db-path $PWD/.mcp-sandbox/test.db" --non-interactive
```

### Direct smoke

```bash
python scripts/mcp_smoke.py sqlite create_table \
    '{"query": "CREATE TABLE smoke (id INTEGER PRIMARY KEY, msg TEXT)"}'
python scripts/mcp_smoke.py sqlite write_query \
    '{"query": "INSERT INTO smoke (msg) VALUES (\"hello from sqlite mcp\")"}'
python scripts/mcp_smoke.py sqlite read_query \
    '{"query": "SELECT * FROM smoke"}'
```

期待値: 3 番目の呼び出しが `[{'id': 1, 'msg': 'hello from sqlite mcp'}]` を返す。

### Usage from chat

```bash
echo 'mcp.sqlite: true' >> .reyn/approvals.yaml

# (任意) 以前 sqlite とやり取りしている場合は履歴をクリーンに:
echo -n > .reyn/agents/default/history.jsonl

reyn chat
> Create a `notes` table in sqlite with columns id and body, then
> insert a row with body "first note", and show me everything in the table.
```

エージェントは 1 ターン内で 3 つの `mcp__call_tool` 呼び出し（= `sqlite__create_table` → `sqlite__write_query` → `sqlite__read_query`）を連鎖させます。クリーン履歴での成功率は約 90% です。エージェントが「テーブルを列挙できません…」と言う場合は、履歴を消去（上記の行）してリトライしてください。

### 公開されるツール

`read_query` / `write_query` / `create_table` / `list_tables` /
`describe_table` / `append_insight`。

---

## everything

MCP プロトコルのプリミティブの大半を網羅するデモ「kitchen sink」サーバー。

### Install

```bash
reyn mcp install --source npm:@modelcontextprotocol/server-everything --non-interactive
```

### Direct smoke

```bash
python scripts/mcp_smoke.py everything
python scripts/mcp_smoke.py everything get-sum '{"a": 17, "b": 25}'
python scripts/mcp_smoke.py everything echo '{"message": "hello"}'
```

期待値: 13 ツールが列挙される; sum は "The sum of 17 and 25 is 42." を返す。

### Usage from chat

```bash
echo 'mcp.everything: true' >> .reyn/approvals.yaml

# 任意: 履歴をクリーンに（= sqlite と同じ注意）
echo -n > .reyn/agents/default/history.jsonl

reyn chat
> Use the everything MCP server to compute 17 plus 25.
```

エージェントは `mcp__call_tool({tool: "everything__get-sum", tool_args: {a: 17, b: 25}})` を呼び、結果を報告します。クリーン履歴での成功率は約 90% です。

> 注: プロンプトで「the everything MCP server」と明示するとルーターの曖昧性解消を助けます。汎用的な「compute 17 plus 25」では、LLM がツール呼び出しなしで算術的に答えてしまうことがあります。

### 公開されるツール

`echo` / `get-sum` / `get-env` / `get-tiny-image` /
`get-annotated-message` / `get-structured-content` /
`get-resource-links` / `get-resource-reference` /
`gzip-file-as-resource` / `toggle-simulated-logging` /
`toggle-subscriber-updates` / `trigger-long-running-operation` /
`simulate-research-query`。

`trigger-long-running-operation` は MCP の progress コールバック配線のテストに特に便利です — 実行中に `notifications/progress` を発行します。

---

## さらにサーバーを追加する

stdio トランスポートの任意の MCP サーバーについて:

```bash
reyn mcp install --source npm:<package>           # または pypi:<package>
echo "mcp.<server-name>: true" >> .reyn/approvals.yaml
reyn chat
```

install コマンドはローダーが読み込める設定を自動で書き込みます。認証情報が必要なサーバー: `reyn mcp set-secret <name> <KEY>` + YAML の `env:` ブロックで `${KEY}` を参照 — [リファレンス: `reyn.yaml` § MCP servers](../../reference/config/reyn-yaml.md#mcp-servers) を参照してください。

## トラブルシューティング

| 症状 | 考えられる原因 | 対処 |
|---|---|---|
| サーバーがインストール済みなのにエージェントが「I cannot ...」と言う | 履歴汚染（= 過去の拒否ターン） | `echo -n > .reyn/agents/<name>/history.jsonl` してリトライ。上記の履歴汚染の注意を参照 |
| `MCP server <name> access denied` | permission が未承認 | `echo 'mcp.<name>: true' >> .reyn/approvals.yaml` |
| install 後の `not found` エラー | サーバーが uvx（Python）を使うが `uv` 未インストール | `brew install uv` |
| YAML のサーバー設定に `type: stdio` が無い、または `server-` 接頭辞がある | 古い install 経路 | `reyn mcp install` で再インストール |
| MCP fetch / filesystem / memory をインストールしたのにエージェントが Reyn op を使う | Reyn 内部 op（`web__fetch` / `file__*` / `memory_operation__*`）が自然プロンプトで勝つ | `scripts/mcp_smoke.py` の直接呼び出しを使う; MCP サーバーはチャットルーター経由では動かない |
