---
type: how-to
topic: files
audience: [human]
---

# ローカルファイルを扱う

Reyn はプロジェクト内のファイルを読み、それについての質問に答えられます。特別な構文は不要で、やりたいことを普通の言葉で説明するだけです。

---

## 始める前に: 一度だけの設定

ファイルアクセスは `filesystem` MCP サーバーを経由します。まだなければ、以下のブロックを `reyn.yaml` に追加してください:

```yaml
permissions:
  mcp.filesystem: allow   # 呼び出しごとのプロンプトを省略

mcp:
  servers:
    filesystem:
      type: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
```

`args` 末尾の `.` はサーバーのルートを現在のディレクトリに設定します。そのディレクトリツリー内はすべて読み取り可能で、外側のパスは読めません。

`permissions` 行を省略すると、Reyn は読み取りごとにインタラクティブに承認を求めます。TUI では問題なく動きますが、ヘッドレス環境ではブロックします。

すべてのオプションについては [How-to: Permission を管理する](manage-permissions.md) を参照してください。

---

## 単一ファイルを参照する

ファイルパスは自然に書いてください。Reyn はくだけた説明も正確なパスも理解します:

```
> README を要約して
> pyproject.toml は依存として何を宣言してる?
> src/reyn/runtime.py が何をするか説明して
```

スキルはパスを解決し、回答を組み立てる前にファイルを読みます。応答の最後に、実際に読んだファイルを伝えます。

---

## 複数ファイルを参照する

1 つのリクエストで複数のファイルを指定できます:

```
> docs/concepts/runtime/workspace.md と docs/concepts/runtime/events.md のアプローチを比較して
> src/reyn/models.py と src/reyn/core/op_runtime/registry.py の違いは?
> CHANGELOG.md と pyproject.toml を読んで、どのバージョンか教えて
```

Reyn は 1 ターンあたり最大 5 ファイルを読みます。それ以上必要なリクエストは、続きのターンに分けてください。

---

## ディレクトリについて尋ねる

ディレクトリを指定すると、Reyn は最も関連性の高いエントリポイント（`__init__.py`、`index.md`、`README` など）を選びます:

```
> src/reyn/core/op_runtime/ には何がある?
> docs/concepts/ の下を案内して
```

大きなディレクトリのすべてのファイルを読むことはせず、どのファイルから始めるかを推論します。間違ったものを選んだ場合は、意図したファイルを伝えれば読み直します。

---

## よくあるシナリオ

### ドキュメントを要約する

```
> docs/concepts/architecture/principles.md の philosophy セクションを要約して
> docs/guide/for-users/index.md を 1 段落で概観して
```

### ソースコードを理解する

```
> src/reyn/models.py の `ContextFrame` クラスは何をする?
> src/reyn/core/op_runtime/registry.py の public 関数を列挙して
> src/reyn/runtime.py はどうやって skill run を開始する?
```

### 設定を確認する

```
> reyn.yaml にはどの MCP サーバーが設定されてる?
> このプロジェクトに事前承認された permission はある?
```

### 差分を見つける

```
> CHANGELOG.md に書かれた 2 つのバージョン間で何が変わった?
> これら 2 つの artifact YAML ファイルの input スキーマを比較して
```

---

## このスキルにできないこと

- **ファイルの書き込み・変更** — `read_local_files` は読み取り専用です。ファイルを編集したい場合は明示的にそう伝えてください。ルーターが別のスキルを選びます。
- **サーバールート外のファイルの読み取り** — サーバーをルート `.` で設定した場合、`/etc/passwd` や `~/.ssh/config` のようなパスはスコープ外でエラーを返します。この境界を強制するのは Reyn ではなくサーバーです。
- **バイナリファイルの読み取り** — 基盤となるツールは `read_text_file` です。画像、コンパイル済み成果物、その他のバイナリは未対応です。

---

## トラブルシューティング

**読み取りのたびに「permission denied」**

`reyn.yaml` の `permissions:` ブロックに `mcp.filesystem: allow` を追加するか（上の [設定](#始める前に-一度だけの設定) を参照）、TUI セッション中にインタラクティブなプロンプトへ `[y]` で答えてください。

**「path outside project scope」エラー**

filesystem MCP サーバーのルートは起動時（`args` の最後の引数）に設定されます。パスはそのルートからの相対でなければなりません。絶対パスや `../` でエスケープするパスはサーバーが拒否します。

**Reyn が間違ったファイルを読む**

意図したファイルを伝えてください:

```
> それじゃなくて — src/reyn/core/op_runtime/registry.py のこと
```

スキルが正しいパスを読み直します。

**応答がない、または「skill exited empty」**

`filesystem` サーバーが起動していないか、設定が誤っている可能性があります。`reyn.yaml` に `mcp.servers.filesystem` があること、`npx` がインストールされていること（`npx --version`）を確認してください。

---

## 関連情報

- [How-to: Permission を管理する](manage-permissions.md) — filesystem アクセスの承認・永続化・取り消し
- [Getting started: チャットモード](../getting-started/02-chat-mode.md) — `reyn chat` の基本
