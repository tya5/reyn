---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn mcp serve]
---

# `reyn mcp`

JSON-RPC stdio transport 経由で Reyn agents を外部の MCP 対応 client に公開します。

## 概要

```
reyn mcp serve [--project PATH] [--timeout SECONDS] [共通フラグ]
```

## 説明

`reyn mcp serve` は Reyn を MCP (Model Context Protocol) JSON-RPC server として起動します。Claude Code、Cursor、MCP 対応の OpenAI Agents SDK、その他 MCP プロトコルに対応した任意の外部 client は、`list_agents` と `send_to_agent` の 2 つのツールを使って Reyn agents にメッセージを送信できます。

これは Reyn の MCP client ロール（Reyn がサードパーティの MCP server を呼び出す方向）の逆です。ここでは外部 client が Reyn に呼び込む形になります。`reyn chat` と同じ `reyn.yaml` および agent registry が MCP server のバックエンドとして使われます。Permission チェック、Event 出力、通常の OS バリデーションはすべて動作します。

概念モデルと Two roles フレーミングについては [concepts/mcp.md — Role 2](../../concepts/mcp.md#role-2-mcp-server-external-clients-call-reyn) を参照してください。

## サブコマンド: `serve`

現時点で利用可能な唯一のサブコマンドです。

`reyn mcp serve` は stdio 上で JSON-RPC を話す server を起動します。Port は使いません。MCP client がプロセスを起動し、transport を所有します。Claude Desktop、Cursor、Claude Code などの MCP client は通常 `cwd=/` で server プロセスを起動するため、client 設定の `args` リストに必ず `--project` を渡してください。それなしでは server が `reyn.yaml` を見つけられません。

起動時の動作:

1. Project root から `reyn.yaml` を読み込み、agent registry をロードします。
2. WAL を per-agent スナップショットに replay し、実行中だった skill が再開できるようにします（`reyn chat` の起動と同じ動作）。
3. MCP JSON-RPC ループに入り、tool 呼び出しを待ちます。

stdin の EOF（MCP client が切断）を受け取ると、registry をクリーンにシャットダウンし、実行中のセッションをすべてドレインします。

server は非インタラクティブに動作します。MCP transport が所有する stdin には人間がいないため、インタラクティブな Permission プロンプトは無期限にブロックします。server を接続する前に、`reyn.yaml` で `permissions: allow` を設定して Skill の Permission を事前承認してください。

## オプション

| フラグ | デフォルト | 説明 |
|------|---------|-------------|
| `--project PATH` | `reyn.yaml` を含む最も近い親ディレクトリ。見つからない場合は exit code 1 で失敗 | Project root。MCP client はプロセス起動時に `cwd` フィールドを無視するため、ほとんどの client 設定で必須です。 |
| `--timeout SECONDS` | `60.0` | `send_to_agent` 呼び出しごとの最大ブロック時間。タイムアウト時は、それまでに蓄積した返信を返します。agent はバックグラウンドで作業を続け、次の `send_to_agent` 呼び出しで残りを受け取れます。 |

共通フラグ（`--model`、`--output-language`、`--max-phase-visits` 等）も使用できます。[common-flags.md](common-flags.md) を参照してください。

## 公開される Tools

`reyn` という server 名で 2 つの MCP tool が登録されます。

### `list_agents()`

`reyn.yaml` で宣言された agent ごとのオブジェクト配列を JSON で返します。

```json
[
  {"name": "default", "role": "汎用アシスタント"},
  {"name": "researcher", "role": "ドメイン調査と合成"}
]
```

| フィールド | 型 | 説明 |
|-------|------|-------------|
| `name` | string | `reyn.yaml` で宣言または `reyn agent new` で作成された agent 名。 |
| `role` | string | `profile.yaml` の role 説明の最初の行。role がない場合は空文字列。 |

### `send_to_agent(agent_name, message)`

指定した agent にユーザーメッセージを送信し、最終返信まで（最大 `--timeout` 秒）ブロックします。

| パラメータ | 型 | 説明 |
|-----------|------|-------------|
| `agent_name` | string | Agent 名。`list_agents` で利用可能な agent を列挙できます。 |
| `message` | string | ユーザーメッセージ本文。 |

JSON オブジェクトを返します。

```json
{"reply": "...", "partial": false, "agent": "default"}
```

| フィールド | 型 | 説明 |
|-------|------|-------------|
| `reply` | string | Agent の返信テキスト。`partial=true` でタイムアウト前に返信がなかった場合は説明的なプレースホルダーが入ります。 |
| `partial` | boolean | agent がアイドルになる前にタイムアウトした場合 `true`。agent のタスクはバックグラウンドで継続します。再度呼び出すことで残りを受け取れます。 |
| `agent` | string | `agent_name` パラメータのエコー。 |

呼び出しをまたいだマルチターン継続性が保たれます。各 agent の `ChatSession` は呼び出し間で `history.jsonl` を永続化します。`reyn mcp serve` 経由で開始した会話は `reyn chat` から再開でき、逆も同様です。

## 例

カレントディレクトリのプロジェクトに対して MCP server を起動:

```bash
reyn mcp serve
```

明示的な project パスを指定して起動（ほとんどの MCP client 設定で必須）:

```bash
reyn mcp serve --project /path/to/your/project
```

長時間の agent ターンに対応するためにタイムアウトを延長:

```bash
reyn mcp serve --project /path/to/your/project --timeout 180
```

Claude Code の `mcp.json` への組み込み（stdio transport）:

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

## Exit codes

| コード | 意味 |
|------|---------|
| `0` | クリーンなシャットダウン — stdin の EOF（MCP client が切断）。 |
| `1` | 設定エラー — `reyn.yaml` が見つからない、または WAL のスキーマバージョン不一致。 |
| その他 | 予期しない例外。 |

## 関連情報

- [コンセプト: MCP — Two roles](../../concepts/mcp.md) — 概念モデル
- [リファレンス: reyn chat](chat.md) — インタラクティブな REPL の代替
- [リファレンス: reyn.yaml](../config/reyn-yaml.md) — agent 設定と MCP server 宣言
- [リファレンス: 共通フラグ](common-flags.md) — CLI コマンド共通フラグ
- [リファレンス: permissions](../config/permissions.md) — 非インタラクティブ使用向けの Skill 事前承認
