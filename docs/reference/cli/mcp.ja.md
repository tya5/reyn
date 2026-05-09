---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn mcp]
---

# `reyn mcp`

MCP サーバーの設定を管理し、Reyn エージェントを外部の MCP 対応クライアントに公開します。

## 概要

```
reyn mcp serve     [--project PATH] [--timeout SECONDS] [共通フラグ]
reyn mcp search    <QUERY>
reyn mcp install   <SERVER_ID> [--scope SCOPE] [--env KEY=VALUE ...] [--non-interactive]
reyn mcp list      [--probe]
reyn mcp remove    <NAME> [--scope SCOPE]
reyn mcp set-secret <SERVER> <KEY>[=<VALUE>]
reyn mcp clear-secret <SERVER> [<KEY>]
```

## 概要

`reyn mcp` は 2 種類の操作をひとつのコマンドにまとめています：

- **アウトバウンドサーバー管理** — `search`、`install`、`list`、`remove`、`set-secret`、`clear-secret` は reyn がクライアントとして呼び出す MCP サーバーを管理します。
- **インバウンドサーバーモード** — `serve` は reyn 自身のエージェントを外部の MCP クライアントに公開します。

概念モデルと Two roles フレーミングについては [コンセプト: MCP](../../concepts/mcp.md) を参照してください。

---

## サブコマンド: `search`

MCP サーバーレジストリで利用可能なサーバーを検索します。

```
reyn mcp search <QUERY>
```

MCP レジストリ API（`registry.modelcontextprotocol.io`）にクエリを送ります。オフライン耐性のためローカルキャッシュ（`~/.reyn/registry-cache/`、TTL 24h）を使用します。

```bash
reyn mcp search "github"
reyn mcp search "filesystem"
reyn mcp search "ファイル操作"
```

**出力：** 名前、説明、ランタイムヒント、インストールコマンドのプレビューを含むマッチしたサーバーの表形式リスト。この出力のサーバー識別子を `reyn mcp install` に渡して使用します。

---

## サブコマンド: `install`

MCP サーバーを reyn の設定にインストールします。

```
reyn mcp install <SERVER_ID> [--scope SCOPE] [--env KEY=VALUE ...] [--non-interactive]
reyn mcp install --source <SOURCE_SPEC> [--scope SCOPE] [--env KEY=VALUE ...] [--non-interactive]
```

`install` は新しい MCP サーバーへの推奨された最初のステップです。 2 つの path:

**レジストリ path** (= default、 `<SERVER_ID>` を渡す):

1. レジストリからサーバーの `server.json` を取得します（`registry.modelcontextprotocol.io`）。
2. 必要なランタイム（`npx`、`uvx`、`docker` など）がインストールされているか確認します。
3. `mcp_install` パーミッションゲートを適用します（[パーミッションとの連動: mcp_install](#permission-interaction) 参照）。
4. 必要な認証情報（レジストリマニフェストで `isSecret` とマークされているもの）をプロンプトするか、`--env` フラグから読み取ります。
5. 認証情報の値を `~/.reyn/secrets.env` に保存します（[コンセプト: シークレット管理](../../concepts/secret-handling.md) 参照）。
6. 対象スコープの設定ファイルに `mcp.servers.<name>` エントリを書き込みます（シークレットは `${VAR}` 参照として記述されます）。
7. `mcp_server_installed` 監査イベントを発行します。

**ソース path** (= `--source <SOURCE_SPEC>`、 レジストリに未登録のサーバー向け、 Anthropic 公式 reference servers `@modelcontextprotocol/server-filesystem` 等):

レジストリ取得を完全に skip し、 ソース指定子から install metadata を解決します。 パーミッションゲート / 認証情報 / 設定書込 / 監査 event はレジストリ path と同一。

サポートする source scheme:

| Scheme | 例 | 解決先 |
|--------|---------|-------------|
| `npm:<package>[@version]` | `npm:@modelcontextprotocol/server-filesystem` | `command: npx, args: ["-y", "<package>"]` |
| `pypi:<package>[==version]` | `pypi:mcp-server-fetch` | `command: uvx, args: ["<package>"]` |
| `docker:<image>[:tag]` | `docker:mcp/playwright:latest` | `command: docker, args: ["run", "--rm", "-i", "<image>"]` |
| `https://github.com/<owner>/<repo>[/...]` | `https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem` | ヒューリスティック: 既知 repo は `@scope/<package>` npm パッケージに解決、 未知 repo は `command` なしで設定書込 (= silent bad install を回避し、 ランタイム時に明示的失敗) |

`<SERVER_ID>` と `--source` は相互排他です。

### オプション

| フラグ | デフォルト | 説明 |
|------|---------|-------------|
| `--scope SCOPE` | `local` | 書き込む設定スコープ: `local`（`.reyn/config.yaml`、gitignored）、`project`（`reyn.yaml`、コミット対象）、または `user`（`~/.reyn/config.yaml`）。 |
| `--source <SPEC>` | — | レジストリ経由でなく直接ソース指定子（`npm:`、`pypi:`、`docker:`、`https://github.com/...`）からインストール。`<SERVER_ID>` と相互排他。 |
| `--env KEY=VALUE` | — | 環境変数をあらかじめ指定します（繰り返し可）。そのキーのインタラクティブプロンプトを抑制します。 |
| `--non-interactive` | off | すべてのインタラクティブプロンプトを抑制します。必要な認証情報が不足している場合はゼロ以外で終了します。CI 用。 |

### スコープのガイドライン

| スコープ | ユースケース |
|-------|----------|
| `local`（デフォルト） | 個人/実験的 — チームメンバーに影響を与えずにサーバーを試す。 |
| `project` | チーム共有 — 全チームメンバーがサーバーを利用できる。シークレットはコミットされた設定に `${VAR}` 参照として残り、実際の値は各開発者の `~/.reyn/secrets.env` に留まる。 |
| `user` | プロジェクト横断 — すべてのプロジェクトで使いたいサーバー（例: `filesystem`）。 |

### 例

```bash
# 利用可能なサーバーを検索
reyn mcp search "github"

# インタラクティブな認証情報プロンプトでインストール
reyn mcp install io.github.modelcontextprotocol/server-github

# 認証情報をインラインで指定してインストール（CI）
reyn mcp install io.github.modelcontextprotocol/server-github \
  --env GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxxx \
  --non-interactive

# プロジェクトスコープにインストール（チーム共有設定）
reyn mcp install io.github.modelcontextprotocol/server-github --scope project

# Anthropic 公式サーバー (= レジストリ未登録) を npm ソース経由で install
reyn mcp install --source npm:@modelcontextprotocol/server-filesystem

# PyPI ソース経由で install
reyn mcp install --source pypi:mcp-server-fetch

# Docker ソース経由で install
reyn mcp install --source docker:mcp/playwright

# GitHub URL 経由 (= heuristic resolver)
reyn mcp install --source https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem
```

### パーミッションとの連動: mcp_install {#permission-interaction}

ディスクに書き込む前に、`install` は `mcp_install` パーミッションゲートをチェックします（ADR-0029）。デフォルトの動作は `ask` です。初回インストール時にプロンプトが表示されます：

```
[approval] MCP サーバー 'io.github.modelcontextprotocol/server-github' をインストールしますか？

  [y] このインストールのみ許可
  [j] このサーバーの承認を永続化
  [r] 今後のすべてのインストールを許可
  [N] 拒否
```

エンタープライズチームは `reyn.yaml` で `permissions.mcp_install: deny` を設定してサーバーの追加を防ぐか、`allow` でプロンプトを完全にスキップできます。詳細は [コンセプト: パーミッションモデル](../../concepts/permission-model.md) を参照してください。

---

## サブコマンド: `list`

設定済み MCP サーバーとそのステータスを一覧表示します。

```
reyn mcp list [--probe]
```

デフォルトでは設定ファイルのみを読み取ります（ネットワーク呼び出しなし、サブプロセス起動なし）：

```
NAME         TRANSPORT  STATUS         CREDENTIALS
filesystem   stdio      ready          (none)
github       stdio      ready          GITHUB_PERSONAL_ACCESS_TOKEN ✓ (set)
slack        stdio      missing-cred   SLACK_BOT_TOKEN ✗ (not set)
```

| フラグ | 説明 |
|------|-------------|
| `--probe` | 各サーバーとハンドシェイクして動作確認します。低速です。実際のサブプロセス起動とネットワーク呼び出しが発生します。`mcp_probe_called` 監査イベントが追加されます。 |

---

## サブコマンド: `remove`

設定から MCP サーバーを削除します。

```
reyn mcp remove <NAME> [--scope SCOPE]
```

指定した（または推論した）スコープの設定ファイルから `mcp.servers.<name>` エントリを削除します。`~/.reyn/secrets.env` には**触れません**。同じキーを使用している他のサーバーのために認証情報は引き続き利用可能です。

| フラグ | デフォルト | 説明 |
|------|---------|-------------|
| `--scope SCOPE` | 自動検出 | 削除するスコープ層。省略した場合、サーバーが存在するスコープから削除します（local を先に、次に project、次に user）。 |

注: すでにサーバーに接続している実行中の `reyn chat` サブプロセスは、セッションが終了するまで継続します。変更は次の reyn プロセス起動時に有効になります。

---

## サブコマンド: `set-secret`

設定済み MCP サーバーの認証情報を設定します。

```
reyn mcp set-secret <SERVER> <KEY>[=<VALUE>]
```

`set-secret` は `reyn secret set` の MCP-aware な薄いラッパーです。サーバーの `mcp.servers.<name>.env` 宣言（またはレジストリの `server.json`）を読んで適切なキー名を提案し、ユニバーサルシークレットストア経由で `~/.reyn/secrets.env` に値を保存します。

`set-secret` を使う場面：

- `install` 時にスキップした認証情報を追加する。
- 特定のサーバーの既存認証情報をローテーションする。

```bash
# インタラクティブ（非表示入力）
reyn mcp set-secret github GITHUB_PERSONAL_ACCESS_TOKEN

# インラインの値
reyn mcp set-secret github GITHUB_PERSONAL_ACCESS_TOKEN=ghp_new_token
```

ストレージはユニバーサルです。`reyn secret set GITHUB_PERSONAL_ACCESS_TOKEN=...` でも同じ結果になります。

---

## サブコマンド: `clear-secret`

設定済み MCP サーバーの認証情報を削除します。

```
reyn mcp clear-secret <SERVER> [<KEY>]
```

| 引数 | 説明 |
|----------|-------------|
| `SERVER` | `mcp.servers.*` で宣言されているサーバー名。 |
| `KEY` | 削除するシークレットキー。省略した場合、サーバーに宣言されているすべてのシークレットをクリアします。 |

```bash
# 特定の認証情報をクリア
reyn mcp clear-secret github GITHUB_PERSONAL_ACCESS_TOKEN

# サーバーのすべての認証情報をクリア
reyn mcp clear-secret slack
```

---

## サブコマンド: `serve`

JSON-RPC stdio transport 経由で Reyn agents を外部の MCP 対応 client に公開します。

```
reyn mcp serve [--project PATH] [--timeout SECONDS] [共通フラグ]
```

`reyn mcp serve` は Reyn を MCP (Model Context Protocol) JSON-RPC server として起動します。Claude Code、Cursor、MCP 対応の OpenAI Agents SDK、その他 MCP プロトコルに対応した任意の外部 client は、`list_agents` と `send_to_agent` の 2 つのツールを使って Reyn agents にメッセージを送信できます。

これは Reyn の MCP client ロール（Reyn がサードパーティの MCP server を呼び出す方向）の逆です。ここでは外部 client が Reyn に呼び込む形になります。`reyn chat` と同じ `reyn.yaml` および agent registry が MCP server のバックエンドとして使われます。Permission チェック、Event 出力、通常の OS バリデーションはすべて動作します。

概念モデルと Two roles フレーミングについては [コンセプト: MCP](../../concepts/mcp.md) の「ロール 2」セクションを参照してください。

`reyn mcp serve` は stdio 上で JSON-RPC を話す server を起動します。Port は使いません。Claude Desktop、Cursor、Claude Code などの MCP client は通常 `cwd=/` で server プロセスを起動するため、client 設定の `args` リストに必ず `--project` を渡してください。それなしでは server が `reyn.yaml` を見つけられません。

起動時の動作:

1. Project root から `reyn.yaml` を読み込み、agent registry をロードします。
2. WAL を per-agent スナップショットに replay し、実行中だった skill が再開できるようにします（`reyn chat` の起動と同じ動作）。
3. MCP JSON-RPC ループに入り、tool 呼び出しを待ちます。

stdin の EOF（MCP client が切断）を受け取ると、registry をクリーンにシャットダウンし、実行中のセッションをすべてドレインします。

server は非インタラクティブに動作します。MCP transport が所有する stdin には人間がいないため、インタラクティブな Permission プロンプトは無期限にブロックします。server を接続する前に、`reyn.yaml` で `permissions: allow` を設定して Skill の Permission を事前承認してください。

### `serve` オプション

| フラグ | デフォルト | 説明 |
|------|---------|-------------|
| `--project PATH` | `reyn.yaml` を含む最も近い親ディレクトリ。見つからない場合は exit code 1 で失敗 | Project root。MCP client はプロセス起動時に `cwd` フィールドを無視するため、ほとんどの client 設定で必須です。 |
| `--timeout SECONDS` | `60.0` | `send_to_agent` 呼び出しごとの最大ブロック時間。タイムアウト時は、それまでに蓄積した返信を返します。agent はバックグラウンドで作業を続け、次の `send_to_agent` 呼び出しで残りを受け取れます。 |

共通フラグ（`--model`、`--output-language`、`--max-phase-visits` 等）も使用できます。[common-flags.md](common-flags.md) を参照してください。

### 公開される Tools

`reyn` という server 名で 2 つの MCP tool が登録されます。

#### `list_agents()`

`reyn.yaml` で宣言された agent ごとのオブジェクト配列を JSON で返します：

```json
[
  {"name": "default", "role": "汎用アシスタント"},
  {"name": "researcher", "role": "ドメイン調査と合成"}
]
```

#### `send_to_agent(agent_name, message)`

指定した agent にユーザーメッセージを送信し、最終返信まで（最大 `--timeout` 秒）ブロックします。

返値：

```json
{"reply": "...", "partial": false, "agent": "default"}
```

`partial=true` の場合、agent がアイドルになる前にタイムアウトした状態です。再度呼び出すことで残りを受け取れます。マルチターンの継続性は保たれます。各 agent の `ChatSession` は呼び出し間で `history.jsonl` を永続化します。

### `serve` の例

```bash
# カレントディレクトリのプロジェクトに対して起動
reyn mcp serve

# 明示的な project パスを指定（ほとんどの MCP client 設定で必須）
reyn mcp serve --project /path/to/your/project

# 長時間の agent ターンに対応するためにタイムアウトを延長
reyn mcp serve --project /path/to/your/project --timeout 180
```

Claude Code の `mcp.json` への組み込み（stdio transport）：

```json
{
  "mcpServers": {
    "reyn": {
      "command": "/absolute/path/to/venv/bin/reyn",
      "args": [
        "mcp", "serve",
        "--project", "/absolute/path/to/your/reyn-project"
      ]
    }
  }
}
```

---

## Exit codes

| コード | 意味 |
|------|---------|
| `0` | 成功 / クリーンなシャットダウン。 |
| `1` | 設定エラー — `reyn.yaml` が見つからない、権限がない、WAL のスキーマバージョン不一致。 |
| その他 | 予期しない例外。 |

## 関連情報

- [コンセプト: MCP](../../concepts/mcp.md) — 概念モデル、2 つのロール、セキュリティモデル
- [コンセプト: シークレット管理](../../concepts/secret-handling.md) — `~/.reyn/secrets.env` と `${VAR}` interpolation
- [コンセプト: パーミッションモデル](../../concepts/permission-model.md) — `mcp_install` パーミッション
- [Reference: `reyn secret`](secret.md) — ユニバーサルシークレット管理
- [Reference: `reyn.yaml`](../config/reyn-yaml.md) — `mcp.servers:` スキーマと `permissions.mcp_install:`
- [Reference: 共通フラグ](common-flags.md) — CLI コマンド共通フラグ
- [How-to: MCP サーバーを使う](../../guide/for-skill-authors/use-an-mcp-server.md)
