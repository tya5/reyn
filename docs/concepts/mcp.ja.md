---
type: concept
topic: integration
audience: [human, agent]
---

# MCP (Model Context Protocol)

reyn は MCP を双方向で実装しています。外部の MCP サーバーを呼び出すクライアントとして機能し、かつ自分自身のエージェントを外部の LLM クライアントに公開するサーバーとしても機能します。この 2 つのロールは独立しており、どちらも実装済みです。

## MCP とは

MCP は AI エージェントがツールを公開する「サーバー」に接続するための JSON-RPC プロトコルです。仕様は Anthropic が [modelcontextprotocol.io](https://modelcontextprotocol.io) で公開しています。公式のサーバー実装（`filesystem`、`git`、`github`、`fetch`、`brave-search` など）が多数あり、サードパーティも多数提供しています。サーバーはツールリスト（`tools/list`）を公開し、呼び出し（`tools/call`）を実行します。エージェント側はベンダー非依存のままです。

要点：Skill は「`filesystem` サーバーの `read_text_file` ツールを呼んでほしい」と宣言するだけです。「`cat` を実行してほしい」とは言いません。バックエンドを差し替えるのは設定変更であり、コード変更ではありません。

## Reyn が担う 2 つのロール

| ロール | 方向 | 仕組み |
|--------|------|--------|
| **MCP クライアント** — Reyn が外部サーバーを呼ぶ | アウトバウンド | Phase の `mcp` Control IR op + `permissions.mcp:` 宣言。Skill は「このサーバーのこのツールを呼んでほしい」と指示し、OS が `MCPClient`（stdio / http / sse）経由でディスパッチします。例：Skill が `filesystem` MCP サーバーを通じてファイルを読む。 |
| **MCP サーバー** — 外部クライアントが Reyn を呼ぶ | インバウンド | `reyn mcp serve --project .` を実行すると Reyn が JSON-RPC サーバーになります。Claude Code、Cursor、OpenAI Agents SDK など MCP に対応した任意のクライアントが、`list_agents()` と `send_to_agent(agent_name, message)` の 2 つのツールを通じて Reyn のエージェントを呼び出せます。 |

このページでは以降、各ロールを順に解説します。

## クイックスタート: 3 つのコマンドでゼロから MCP を動かす

初回利用時の推奨フローは `reyn mcp install` です。YAML の手動編集は不要です：

```bash
# 1. 利用可能なサーバーを検索
reyn mcp search "github"

# 2. インストール（設定 + 認証情報 + パーミッションゲートを一括処理）
reyn mcp install io.github.modelcontextprotocol/server-github

# 3. すぐに使い始める
reyn chat
> このリポジトリの最近の PR を一覧して
```

`reyn mcp install` は MCP レジストリからサーバーマニフェストを取得し、必要なランタイム（`npx`、`uvx` など）がインストールされているか確認し、認証情報をプロンプト（`~/.reyn/secrets.env` に安全に保存）し、シークレットを `${VAR}` 参照として設定に書き込みます。これらをすべて 1 ステップで実行します。

**レジストリに未登録のサーバー** (= Anthropic 公式 reference servers `@modelcontextprotocol/server-filesystem` 等) は `--source` を使います:

```bash
reyn mcp install --source npm:@modelcontextprotocol/server-filesystem
reyn mcp install --source pypi:mcp-server-fetch
reyn mcp install --source docker:mcp/playwright
reyn mcp install --source https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem
```

`--source` はレジストリ取得を skip し、 ソース指定子から install metadata を直接解決します。 パーミッションゲート / 認証情報 / 設定書込 / 監査 event はレジストリ path と同一です。

完全な `reyn mcp` CLI リファレンスは [Reference: `reyn mcp`](../reference/cli/mcp.md) を参照してください。

## クイックスタート: `reyn chat` から MCP を試す（手動設定パス）

手動でサーバーを設定したい場合や、公開レジストリにないサーバーを追加する場合は、`reyn.yaml` に直接追加します。 サーバーを設定すると、 `reyn chat` は `mcp` category の verb actions を自動的に使えます (issue #879 で旧 `mcp.server` / `mcp.tool` / `mcp.operation` 3 sub-category を collapse、 2026-05-25 で install を source 軸で 3 verb に分割):

| Action | 何をするか |
|------|-----------|
| `mcp__search_registry({text})`                              | 公式 MCP registry で新規サーバーを検索 |
| `mcp__install_registry({server_id})`                        | 公式 MCP registry の server を install |
| `mcp__install_package({kind, identifier, version?})`        | npm / pypi / docker / GitHub URL から install |
| `mcp__install_local({name, command, args})`                 | local command (LLM 生成 script 等) を直接 MCP server として登録 |
| `mcp__list_servers()`                                       | `.reyn/mcp.yaml` に設定された全サーバー名を返す |
| `mcp__list_tools({server})`                                 | 1 サーバーが露出する tool 一覧を `{name: "<server>__<tool>", description, inputSchema}` 形式で返す |
| `mcp__call_tool({tool, args})`                              | `<server>__<tool>` ID + tool の declared args で tool を call |
| `mcp__drop_server({server})`                                | install 済サーバーを config から削除 |

LLM router がチャット turn 内で直接これらを呼べます。 初回利用の典型 flow:

```sh
# 1. reyn.yaml にサーバーエントリを追加 (1 回のみ)
mcp:
  servers:
    filesystem:
      type: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]

# 2. reyn.yaml で事前承認、 もしくは初回プロンプトで承認
permissions:
  mcp:
    filesystem: allow

# 3. あとは普通にチャット
reyn chat
> このディレクトリにある README.md を要約して
```

router が自動的に `mcp__list_tools` → `mcp__call_tool` を呼び出します。 どの skill にも `permissions.mcp:` 宣言を書く必要はありません。 **Skill 作成は、 繰り返し使うワークフローを形式化したい時** (= phase graph / validation / retry policy が必要になった時) に検討するものであって、 MCP を使う前提条件ではありません。 以下の deep-dive はその場合の話で、 ad-hoc 利用だけならここで読み終えて問題ありません。

## ロール 1：MCP クライアント — Reyn が外部サーバーを呼ぶ

Skill が外部ツールを必要とするときの流れは次のとおりです：

```
phase frontmatter         LLM が Control IR を発行    OS がディスパッチ
  permissions:        →     {kind: mcp,           →   MCPClient
    mcp: [filesystem]        server: filesystem,        (stdio | http | sse)
                             tool: read_text_file,
                             args: {path: ...}}
```

1. Skill の Phase は frontmatter で `permissions.mcp: [server_name]` を宣言します。宣言がなければ、ランタイムはそのサーバーへのすべての呼び出しを拒否します。
2. LLM は `mcp` Control IR op として `{server, tool, args}` を発行します。サーバー名を勝手に作ることはできません。`reyn.yaml` で設定され Phase のパーミッションに宣言されたサーバーだけが到達可能です。
3. OS はサーバーのトランスポート（`stdio`、`http`、`sse`）を解決し、`MCPClient` 経由でディスパッチして、ツールの結果を Phase ループに返します。
4. 呼び出しごとに event が発行されます。呼び出し前に `mcp_called`、正常終了後に `mcp_completed`（またはエラー時に `mcp_failed`）。監査証跡は他の op と同一です。

境界が明確なのは意図的です。Skill は何が必要かを記述し、OS がどう取得するかを決めます。新しい MCP サーバーを追加しても OS コードには一切触れません（P7）。

## トランスポートの選択（stdio vs HTTP）

公式の MCP サーバーの大多数は stdio 経由で起動するローカルプロセスです。一部のホスト型サービスは HTTP エンドポイントを公開しています。SSE トランスポートは将来のリリース向けです。

| トランスポート | 用途 | reyn の起動方法 |
|--------------|------|----------------|
| `stdio` | ローカル CLI サーバー（大多数の公式サーバー — `filesystem`、`git`、`github`、`fetch`） | `command` を `args` と `env` 付きで起動し、stdin/stdout 越しに JSON-RPC を話す |
| `http` | ホスト型サービス（自前バックエンド、組織内ツールレジストリ） | `url` に `headers` 付きで POST し、実行中はセッションを再利用 |
| `sse` | HTTP のストリーミング変種。用途はまれ | `http` と同様にイベントストリームを追加 |

`npx` や `pip install` でローカル実行するものには `stdio` を選んでください。サーバーを他者が運用していて URL を渡されている場合は `http` を選んでください。

## 設定

MCP サーバーは `reyn.yaml` の `mcp.servers:` 配下で宣言します。各エントリには `type` が必須で、残りはトランスポートによって異なります。

```yaml
# reyn.yaml
mcp:
  servers:
    # stdio: ローカルプロセス、stdin/stdout 越しに JSON-RPC を話す
    filesystem:
      type: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
      env:
        # 任意。${VAR} は起動時に os.environ から展開されます。
        FS_LOG_LEVEL: "info"

    # http: ホスト型サーバー、Streamable HTTP 越しの JSON-RPC
    internal_tools:
      type: http
      url: https://tools.example.internal/mcp
      headers:
        Authorization: "Bearer ${INTERNAL_TOOLS_TOKEN}"
```

| フィールド | stdio | http | 説明 |
|-----------|-------|------|------|
| `type` | 必須 | 必須 | `stdio` \| `http` \| `sse` |
| `command` | 必須 | — | 起動する実行ファイル（例：`npx`、`python`、絶対パス） |
| `args` | 任意 | — | `command` に渡す引数リスト |
| `env` | 任意 | — | 起動プロセスへの追加環境変数 |
| `url` | — | 必須 | エンドポイント URL |
| `headers` | — | 任意 | 静的ヘッダー。値は `${VAR}` 展開に対応 |

`${VAR}` の展開は `os.environ` から解決されます（起動時に `~/.reyn/secrets.env` から事前ロードされています。詳細は [コンセプト: シークレット管理](secret-handling.md) 参照）。変数が存在しない場合は `""` に展開され警告が出ますが、ハードエラーにはなりません。オプショナルなトークンが欠けていても実行はクラッシュしません。

`${VAR}` 構文は `mcp.servers` だけでなく、**すべての** YAML 文字列フィールドで使えます。`models.<name>.api_key`、`litellm.api_base` などすべてが同じ仕組みを使います。全体像は [Reference: `reyn.yaml` — `${VAR}` interpolation](../reference/config/reyn-yaml.md#var-interpolation) を参照してください。

API キーとトークンは `~/.reyn/secrets.env`（`reyn secret set` で管理）に置き、`reyn.yaml` では `${VAR}` として参照してください。リテラルの値をインラインで書かないでください。詳細は [コンセプト: シークレット管理](secret-handling.md) を参照してください。

## セキュリティモデル

MCP の操作は 2 つのポイントでゲートされます：

### インストール時ゲート: `file.write` + `http.get`

MCP サーバーを設定に追加する際、install op の書き込みは OS の標準 list-axis gate を通ります。 旧 `permissions.mcp_install: ask | allow | deny` bool 軸は #571 collapse arc (Phase 5、 2026-05-23) で撤去され、 install 制御は以下の経路に統一されました:

- `.reyn/mcp.yaml` への `file.write` (= canonical mutation target)。 `startup_guard` が skill+path ごとに 1 回 operator に prompt、 承認後の runtime は silent。
- `registry.modelcontextprotocol.io` への `http.get` (= registry fetch)。 同じ prompt model。
- registry が `isSecret` 指定する env-var key への `secret.write` (= key set が runtime 決定なので wildcard `"*"`)。

エンタープライズチームは 2 つの等価な mechanism で private / corporate registry を指定:

**A. `reyn.yaml mcp.registries:` list config** — declarative、 project-scoped、 version-controlled:

```yaml
# reyn.yaml (project scope — committed to git)
mcp:
  registries:
    - https://mcp-registry.internal.acme.com   # private registry (= 最初に試行)
    - https://registry.modelcontextprotocol.io  # public fallback
permissions:
  web.fetch: allow      # registry fetch の blanket allow
  file.write: allow     # .reyn/mcp.yaml 書き込みの blanket 承認
```

**B. `REYN_MCP_REGISTRY_URLS` (plural) env var** — explicit operator override、 CI / per-shell config 用途:

```bash
# operator shell rc / systemd unit / CI runner env
export REYN_MCP_REGISTRY_URLS="https://mcp-registry.internal.acme.com,https://registry.modelcontextprotocol.io"
```

両方 set されている場合 env var が勝つ (= explicit operator override が declarative config を override)。 legacy 単数形 `REYN_MCP_REGISTRY_URL` は単一要素 list として backward compat 維持。

async op-handler client (`reyn.registry.client`) と safe-mode skill-internal lookup (`reyn.safe.mcp.registry`) の両方が list を順次 iterate:

| Operation | 挙動 |
|---|---|
| `lookup(server_id)` | 順次試行、 first non-404 hit を返却、 全 404 → `None`、 404 後の non-404 error は re-raise |
| `search(query)` | 順次試行、 first non-empty result を返却、 全 empty → `[]` |

「private first, public fallback」 pattern: private registry の同名 entry は public を shadow、 public は private に無い名前の discoverability fallback。

詳細は [コンセプト: パーミッションモデル](permission-model.md) → 「Collapse arc」 を参照してください。

> 旧 `permissions.mcp_install: ...` キーは `reyn.yaml` で `DeprecationWarning` 付きで受理され、 migration window 期間中は等価な gate に translate されます。

### ランタイムゲート: `permissions.mcp`

MCP 呼び出しはプロセスを離れる前に 2 つのチェックを通過します：

1. **Phase 宣言。** Phase は frontmatter の `permissions.mcp` に使用したいサーバーを必ずリストアップしなければなりません。ランタイムは `require_mcp(decl, server, ...)` を呼び出し、`server not in decl.mcp` の場合は宣言欠落を明示したエラーで失敗します。
2. **承認。** 他のケイパビリティと同様に、Skill ごとの初回呼び出しでプロンプトが表示されます（`y` / `j` / `r` / `N`）。永続的な承認は `.reyn/approvals.yaml` に `<skill>/mcp.<server>` キーで保存されます。プロジェクト全体を信頼できる場合は `reyn.yaml` で `permissions.mcp: allow` と設定して事前承認できます。

これは reyn の一般的なパーミッションモデルと一致します（[permission-model.md](permission-model.md) 参照）。ある Skill の MCP 承認が別の Skill に漏れることはなく、`run_skill` 経由で起動したサブスキルは独自にパーミッションを要求します。

呼び出しごとに 3 つの監査 event が発行されます：

| Event | タイミング | ペイロード |
|-------|-----------|-----------|
| `mcp_called` | リクエストがプロセスを離れる前 | `server`、`tool`、`args` |
| `mcp_completed` | 正常返却時 | `server`、`tool`、`is_error` |
| `mcp_failed` | トランスポート / プロトコルエラー時 | `server`、`tool`、`error` |

`reyn events tail | grep mcp_` または `grep '"mcp_called"' .reyn/events.jsonl` でフィルタリングできます。

## MCP を使う stdlib Skill

Stdlib Skill では Phase で `permissions.mcp: [<server>]` を宣言し、`tool: <name>`（サーバーが公開するツール名）で `mcp` op を発行し、あとは OS に任せます。独自の MCP バックエンド Skill を作るときの完全なクイックスタートは [how-to](../guide/for-skill-authors/use-an-mcp-server.md) を参照してください。

## ロール 2：MCP サーバー — 外部クライアントが Reyn を呼ぶ

`reyn mcp serve` を実行すると Reyn が MCP サーバーになります。Claude Code、Cursor、OpenAI Agents SDK など MCP プロトコルに対応した任意の外部クライアントが、Reyn のエージェントにメッセージを送れるようになります。

### サーバーの起動

```sh
reyn mcp serve --project /path/to/your/project
```

`--project` は `reyn.yaml` が置かれているディレクトリを指します。MCP クライアントはサーバープロセスを `cwd=/` で起動することが多いため、ほとんどのクライアント設定でこのフラグが必須になります。これがないとサーバーはプロジェクトを見つけられません。`--timeout`（デフォルト 60 秒）は `send_to_agent` が部分的な返答を返すまでブロックする最大時間を制御します。エージェントはタイムアウト後もバックグラウンドで作業を継続します。

サーバーは stdio 越しに JSON-RPC を話します。ポートはありません。MCP クライアント自身がプロセスを起動してトランスポートを所有します。

### 公開されるツール

2 つのツールが登録されます：

| ツール | シグネチャ | 動作 |
|--------|-----------|------|
| `list_agents` | `()` | `reyn.yaml` に登録されたエージェントの `{name, role}` 配列を JSON で返します。 |
| `send_to_agent` | `(agent_name, message)` | 指定したエージェントにユーザー形式のメッセージを 1 件送信し、最終の返答テキストを（`--timeout` 秒まで）ブロックして返します。`{reply, partial, agent}` を返します。`partial=true` の場合はエージェントがまだ作業中です。続きを受け取るには再度呼び出してください。 |

マルチターンの継続性は自動で保たれます。各エージェントの `ChatSession` は呼び出し間も `history.jsonl` を保持するため、Claude Code で始めた会話を `reyn chat` で再開することも、その逆も可能です。

### Skill から見た「MCP 経由」の意味

外部クライアントはエージェントを見るのみで、Skill グラフは見えません。外部から操作できるのは「エージェント一覧の取得」と「メッセージ送信」だけです。Reyn 側では OS コントラクトが通常通り適用されます。パーミッションのチェック、event の発行、バリデーションはすべて実行されます。MCP サーバーは stdin に人間がいない状態で動作するため、対話的なプロンプトは無限にブロックします。`reyn.yaml` で `permissions: allow` を設定すれば非対話的に事前承認できます。

これは Reyn の「外に話しかける + 話しかけられる」マルチエージェントサーフェスの一部です。単一の Reyn プロセス内でエージェント同士がどう関連するかは [multi-agent.md](multi-agent.md) を参照してください。

## MCP が適さない用途

MCP は *外部ケイパビリティへのアクセス* に適したツールです。以下のケースでは使わないでください：

- **重い計算が必要な場合。** Python プリプロセッサー（`python` op）を使ってください。MCP 呼び出しは毎回プロセス境界を越えます。インラインの NumPy ステップの方がはるかに高速です。
- **再利用可能なワークフローを実現したい場合。** それは MCP サーバーではなく Skill です。`skill_builder` で新しい Skill を作ってください。
- **エージェント間メッセージングが必要な場合。** `messages_to_agents` とトポロジールールを使ってください。MCP はエージェントのアイデンティティやチェーンをモデル化しません。
- **呼び出し間の状態が必要な場合。** MCP サーバーはステートレスにもステートフルにもできますが、reyn は各呼び出しを独立したものとして扱います。永続的な状態はワークスペースに置いてください。

これらの用途で MCP を使いたいと思ったら、レイヤーを間違えています。

## 関連項目

- [How-to: MCP サーバーの使い方](../guide/for-skill-authors/use-an-mcp-server.md) — filesystem サーバーのクイックスタート
- [Reference: `reyn mcp`](../reference/cli/mcp.md) — `search`、`install`、`list`、`remove`、`set-secret`、`clear-secret` の完全な CLI リファレンス
- [Reference: `reyn secret`](../reference/cli/secret.md) — ユニバーサルシークレット管理
- [コンセプト: シークレット管理](secret-handling.md) — `~/.reyn/secrets.env` と `${VAR}` interpolation
- [リファレンス: `reyn.yaml`](../reference/config/reyn-yaml.md#mcp-servers) — `mcp.servers:` の完全なスキーマ
- [コンセプト: パーミッションモデル](permission-model.md) — `file.write` / `http.get` / `permissions.mcp` と collapse arc
- [modelcontextprotocol.io](https://modelcontextprotocol.io) — 仕様、サーバーレジストリ、公式 SDK
