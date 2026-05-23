---
type: how-to
topic: integration
audience: [human]
applies_to: [reyn.yaml, mcp.servers, read_local_files]
---

# MCP サーバーを使う

**目的：** [MCP](../../concepts/mcp.md) サーバーを reyn に組み込み、Skill から呼び出せるようにします。公式の `filesystem` サーバーと stdlib の `read_local_files` Skill を実例として使用します — `command` と `args` を変更するだけで他のサーバー（`git`、`github`、`fetch`、`brave-search` など）にも適用できます。

## こんなときに使う

- Workspace のデフォルトゾーン外のファイルを Skill から読み取ったり検索したりしたい。
- カスタムコードを書かずに[公式 MCP サーバー](https://github.com/modelcontextprotocol/servers)を組み込みたい。
- MCP ベースの新しい Skill を作成していて、コピー元の動作確認済みベースラインが欲しい。

## 1. サーバーをインストールする

### 推奨: `reyn mcp install`

MCP レジストリにリストされているサーバーには `reyn mcp install` を使います。サーバーバイナリ、設定、認証情報、パーミッションゲートをすべて自動的に処理します：

```bash
# まずサーバーを検索（任意）
reyn mcp search "filesystem"

# インストール — 1 ステップですべて処理
reyn mcp install io.github.modelcontextprotocol/server-filesystem
```

インストール後、`mcp.servers.filesystem` エントリはすでに設定に追加されています（デフォルトでは `reyn.local.yaml`、または `--scope project` を渡した場合は `reyn.yaml`）。手順 3 に進んでください。

### 高度な使い方: 手動設定

サーバーがパブリックレジストリにない場合や、設定を完全に制御したい場合は手動で追加します。まずサーバーをスタンドアロンで動作確認してください — サーバーと integration の両方を同時にデバッグしたくないはずです：

```bash
# 手動で実行する。サーバー情報を表示して stdin で待機するはずです
npx -y @modelcontextprotocol/server-filesystem .
```

JSON-RPC ハンドシェイクを受け入れたことが確認できたら `Ctrl-C` を押してください。（各 MCP サーバーにはそれぞれのインストールコマンドがあります — サーバーの README を確認してください。`pip`、`cargo`、素のバイナリも一般的な選択肢です。）

## 2. `reyn.yaml` で設定する（手動パス）

`mcp.servers:` ブロックを追加します。短くケバブケースまたはスネークケースの名前を選んでください（`filesystem` が慣例的です）— これが Skill の `permissions.mcp` で宣言し、`mcp` ops で使用する名前になります。

```yaml
# reyn.yaml
mcp:
  servers:
    filesystem:
      type: stdio
      command: npx
      args:
        - "-y"
        - "@modelcontextprotocol/server-filesystem"
        - "."           # サーバーが参照できるルート。絶対パスで範囲を拡大可能
```

認証情報が必要なサーバーの場合は、値を `~/.reyn/secrets.env` に保存して `${VAR}` で参照します：

```bash
# 一度だけ保存
reyn secret set GITHUB_PERSONAL_ACCESS_TOKEN

# reyn.yaml では:
#   env:
#     GITHUB_PERSONAL_ACCESS_TOKEN: ${GITHUB_PERSONAL_ACCESS_TOKEN}
```

HTTP サーバーの場合は `type: http` に切り替えて `url:` と `headers:` を指定します — [Reference: reyn.yaml § MCP servers](../../reference/config/reyn-yaml.md#mcp-servers) を参照してください。

## 3. reyn の MCP extra をインストールする

MCP サポートは最小インストールを軽量に保つためオプション依存として提供されます。

```bash
pip install -e ".[mcp]"
```

これにより公式の `mcp` Python SDK が取り込まれます。reyn は内部でトランスポートとして使用します。<!-- TODO: PR32 がマージされたら extra 名（`[mcp]`）を確認。pyproject.toml では異なるバンドルになる可能性あり。-->

## 4. サンプル Skill を実行する

`read_local_files` stdlib Skill が標準的な呼び出し元です。`reyn chat` セッションから：

```bash
reyn chat
```

```
> README.md を読んで、哲学セクションを要約してください。
```

順番に表示される内容：

1. ルーターが `read_local_files` を選択します（filesystem 系のリクエストがポジティブ例に含まれているため）。
2. `filesystem` サーバーへの最初の呼び出しで承認プロンプトが表示されます：

   ```
   [approval] read_local_files/mcp.filesystem needs:
     MCP server: 'filesystem'

     [y] allow this run only
     [j] persist for this exact path + skill
     [r] persist for the parent dir (recursive) + skill
     [N] deny
   ```

   永続的な承認を残したい場合は `j` を、今回のみ許可する場合は `y` を選択してください。
3. Skill が `mcp` op（`tool: read_text_file`、`args: {path: "README.md"}`）を発行し、OS が stdio 経由でディスパッチして、サーバーがファイル内容を返します。
4. Skill が要求したセクションの散文要約を返信します。

## 5. events で検証する

すべての MCP 呼び出しは監査追跡されます。event ログをテールしてください：

```bash
reyn events tail
```

呼び出しごとに以下が表示されるはずです：

```
mcp_called      server=filesystem tool=read_text_file args={"path":"README.md"}
mcp_completed   server=filesystem tool=read_text_file is_error=false
```

または生ログを grep します：

```bash
grep '"mcp_' .reyn/events.jsonl | tail -n 5
```

サーバーがトランスポートまたはプロトコルエラーを返した場合、`mcp_completed` の代わりに `mcp_failed` が表示されます。

## トラブルシューティング

**`MCP server 'filesystem' is not configured.`** `mcp.servers.filesystem` ブロックが存在しないか名前が違います。`cat reyn.yaml`（または `--scope local` でインストールした場合は `cat reyn.local.yaml`）で確認してください。Skill が使用する名前（`filesystem`）と設定のキーが一致している必要があります。

**`MCP server 'filesystem' not declared in phase permissions.`** Phase のフロントマターに `permissions.mcp: [filesystem]` がありません。Phase ファイルを開いて追加してください。これは設定の問題ではなく、ランタイムのゲートです。

**承認プロンプトが毎回表示される。** `j` / `r` ではなく `y`（ワンショット）を選択しました。再実行して `j` を選択すると永続化されます。あるいは `reyn.yaml` でプロジェクト全体を事前承認します：

```yaml
permissions:
  mcp:
    filesystem: allow
```

**サーバーがすぐにクラッシュする。** `command` + `args` を手動で実行してください（手動パスの手順 1）— 終了せずに stdin を受け付けるはずです。スタンドアロンで失敗する場合は、reyn を再実行する前にインストールを修正してください。クラッシュは基盤となるエラーとともに `mcp_failed` として報告されます。

**`MCP config references undefined environment variable: ${TOKEN}`.** 設定内の `${VAR}` 参照が解決できませんでした。`reyn secret set TOKEN` で値を保存するか、シェルで変数をエクスポートしてください。変数が未定義の場合は空文字列に展開されて失敗ではなく警告になります。

**初回インストール時にパーミッションプロンプトが表示される。** これは期待される動作です — reyn はサーバー追加を標準の list axes（`.reyn/mcp.yaml` への `file.write`、 registry host への `http.get`）でゲートします。 `j` (just this path) または `r` (recursive) で永続化を選択。 非インタラクティブな CI 実行では `--non-interactive` を渡し、 事前に `permissions.file.write: allow` + `permissions.web.fetch: allow` を設定してください。 旧 `mcp_install: ask | allow | deny` キーも migration window 中は動作します（= `DeprecationWarning` を発生）。

**`reyn events tail` に `mcp_called` が表示されない。** Skill が `mcp` op に到達していません — Phase ログを確認して LLM がそれを発行したかどうか確認してください。よくある原因は、パスがプロジェクト内にあるため LLM が `mcp` ではなく `file.read`（デフォルト capability、プロジェクトスコープ）を選択したケースです。これは正しい動作であり、エラーではありません。

## 参考

- [Concepts: MCP](../../concepts/mcp.md) — プロトコル概要、トランスポートの選択、セキュリティモデル
- [コンセプト: シークレット管理](../../concepts/secret-handling.md) — 認証情報のストレージと `${VAR}` interpolation
- [Reference: `reyn mcp`](../../reference/cli/mcp.md) — `install`、`set-secret` などのサブコマンド
- [Reference: `reyn secret`](../../reference/cli/secret.md) — 認証情報の管理
- [Reference: `read_local_files`](../../reference/stdlib/read_local_files.md) — サンプル Skill の詳細
- [Reference: `reyn.yaml` § MCP servers](../../reference/config/reyn-yaml.md#mcp-servers) — 完全なスキーマ
- [How-to: manage permissions](../for-users/manage-permissions.md) — 事前承認、取り消し、eval モード
