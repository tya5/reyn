---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [read_local_files]
---

# `read_local_files`

プロジェクト内のファイル（または設定された `filesystem` MCP サーバーが参照できる任意の場所）を読み取り、その内容に関する質問に回答します — MCP バックの stdlib Skill の標準的な例です。

## 使う場面

- ユーザーがファイル名やパスでファイルについて質問するとき: 「`pyproject.toml` には何が宣言されていますか？」「README のライセンスセクションを要約してください。」
- リクエストがファイルシステム系で、設定済みの `filesystem` サーバーが利用可能な場合、ルーターは `read_local_files` を発行します。

## 使わない場面

- プロジェクト全体にわたる自由形式のコード検索 — それは `web_research` や grep 系の op であり、単一ファイルの読み取りではありません。
- OS がデフォルトの `file.read` パーミッションで直接読み取る `.reyn/` 内のファイル — MCP を経由する必要はありません。
- バイナリファイル。基盤となるツールは `read_text_file` です。

## 必要なセットアップ

### セットアップチェックリスト

1. **MCP filesystem サーバー** — `reyn.yaml` に `mcp.servers.filesystem` を追加します（以下のブロックを参照）。
2. **パーミッションの事前承認** — `reyn.yaml` の `permissions:` ブロックに `mcp.filesystem: allow` を追加します。
   これがないと、Reyn は MCP 呼び出しのたびにインタラクティブなプロンプトを表示します。ヘッドレス / 非 TTY 環境（CI、stdin パイプ、dogfood スクリプト）ではプロンプトに回答できず、すべての呼び出しが `permission_denied` を返し Skill は空の状態で終了します。
3. 完全な動作例は [`cookbook/configs/with-mcp.yaml`](https://github.com/tya5/reyn/blob/main/cookbook/configs/with-mcp.yaml) を参照してください — プロジェクトルートにコピーして `reyn.yaml` にリネームしてください。

`reyn.yaml` の `filesystem` という正確な名前で MCP サーバーが設定されている必要があります。
以下のブロックを既存の `reyn.yaml` に貼り付けてください（`mcp` と `permissions` の両セクションが必要です）。

```yaml
permissions:
  mcp.filesystem: allow   # ヘッドレス / 非 TTY 実行に必要

mcp:
  servers:
    filesystem:
      type: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
```

`[mcp]` extra のインストールを含むセットアップの完全な手順は [How-to: MCP サーバーを使う](../../guide/for-skill-authors/use-an-mcp-server.md) を参照してください。

## Phase

<!-- TODO: read_local_files がランドしたら Phase 名を確認。Skill は並行して作成中。-->

| Phase | 用途 |
|-------|------|
| `read_and_respond` | エントリー Phase。要求されたパスを解決し、`filesystem` に対して `mcp` op を発行し、レスポンスをフォーマットします。`file_content_response` で finish するか、フォローアップのために遷移します。 |

この Phase は frontmatter に `permissions.mcp: [filesystem]` を宣言しています。

## 最終出力: `file_content_response`

| フィールド | 型 | 用途 |
|---------|------|------|
| `path` | string | 読み取ったパス（サーバーが解決したもの） |
| `content` | string | ファイルの内容、またはそれから導出した回答 |
| `summary` | string（省略可能） | ユーザーが生のテキストではなく要約を求めた場合の 1 段落の概要 |

<!-- TODO: 並行実装エージェントと正確なフィールドセットを確認。-->

## 例

このルートにリダイレクトされるプロンプトの例:

- 「README.md を読んで、reyn とは何かを教えてください。」
- 「`LICENSE` に記載されているライセンスは何ですか？」
- 「`docs/concepts/principles.md` の哲学セクションを要約してください。」

このルートに**リダイレクトされない**プロンプトの例:

- 「リポジトリ内のすべての TODO コメントを探してください。」→ より広い検索。単一ファイルの読み取りではありません。
- 「`.reyn/events.jsonl` の中身は何ですか？」→ デフォルトの `file.read` が処理します。MCP 不要。

## ソース

[`src/stdlib/skills/read_local_files/`](https://github.com/tya5/reyn/blob/main/src/stdlib/skills/read_local_files/)

## 関連情報

- [Concepts: MCP](../../concepts/mcp.md) — reyn がプロトコルを統合する仕組み
- [How-to: MCP サーバーを使う](../../guide/for-skill-authors/use-an-mcp-server.md) — この Skill が動かすクイックスタート
- [Reference: `reyn.yaml` § MCP servers](../config/reyn-yaml.md#mcp-servers)
