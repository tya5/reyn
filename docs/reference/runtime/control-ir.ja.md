---
type: reference
topic: runtime
audience: [human, agent]
---

# Control IR

Control IR は LLM が artifact と並行して出力できる副作用 op のリストです。OS は各 op をディスパッチし、LLM（または次の Phase）が消費するために結果を返します。

## Op の種類

| 種類 | 目的 | 必要な Permission |
|------|---------|---------------------|
| `read_file` | ファイルを読み取る（行範囲指定も可） | `file.read` |
| `write_file` | ファイルを書き込む（作成 / 上書き） | `file.write` |
| `edit_file` | ファイル内の文字列を置換する | `file.write` |
| `delete_file` | ファイルを削除する | `file.write` |
| `glob_files` | glob パターンに一致するファイルを列挙する | `file.read` |
| `grep_files` | 正規表現でファイル内容を検索する | `file.read` |
| `ask_user` | Phase を一時停止してユーザーに質問する | なし（常に許可） |
| `sandboxed_exec` | `SandboxPolicy` と `SandboxBackend` を介して argv を実行する(削除済みの `shell` op を置き換え) | バックエンドが強制（`SandboxPolicy`） |
| `web_search` | DuckDuckGo で公開ウェブを検索する | Tier 1 — デフォルト許可；`reyn.yaml` の `web.search: deny` でブロック |
| `web_fetch` | 単一 URL を取得してテキストを抽出する | Tier 1 — デフォルト許可；`reyn.yaml` の `web.fetch: deny` でブロック |
| `mcp` | 設定済み MCP server のツールを呼び出す | Skill frontmatter の `permissions.mcp: [server_name]` |
| `mcp_install` | レジストリから MCP server をプロジェクト設定にインストールする | Skill frontmatter の `permissions.mcp_install: true` |
| `index_query` | インデックス済みソース 1 件に対してセマンティック検索を行う | なし |
| `semantic_search` | マクロ（FP-0057 Phase 2a; `recall` から rename）: embed → 各ソースに index_query → トップ K をマージ | なし |
| `index_drop` | インデックス済みソースを完全削除する（破壊的） | Skill frontmatter の `permissions.index_drop: ask` |

## 共通エンベロープ

すべての op は `kind` ディスクリミネーターを持つ JSON オブジェクトです:

```json
{
  "kind": "read_file",
  "path": "src/foo.py"
}
```

OS は op をその kind のスキーマに対して検証し、実行し、呼び出し元 Phase に結果を返します。

## ファイル op（細粒度）

LLM が発行できるファイル操作は 6 つの細粒度 kind です — chat router がツールとして公開しているのと同じサブセットです（[concepts/architecture/llm-invocation-surfaces.md](../../concepts/architecture/llm-invocation-surfaces.md) を参照）。それぞれ独自のスキーマを持つ独立した op kind であり、`op` サブフィールドはありません。

```json
{"kind": "read_file", "path": "src/foo.py"}
{"kind": "read_file", "path": "src/foo.py", "offset": 100, "limit": 40}

{"kind": "write_file", "path": "out.txt", "content": "..."}

{"kind": "edit_file", "path": "src/foo.py",
 "old_string": "...", "new_string": "...", "replace_all": false}

{"kind": "delete_file", "path": "tmp.txt"}

{"kind": "glob_files", "path": ".", "pattern": "**/*.py", "max_results": 50}

{"kind": "grep_files", "path": "src", "pattern": "def \\w+",
 "glob": "**/*.py", "case_sensitive": false, "max_results": 50}
```

| 種類 | Permission | 備考 |
|------|-----------|-------|
| `read_file` | `file.read` | `offset` / `limit`（行範囲）は省略可。 |
| `write_file` | `file.write` | 作成または上書き；親ディレクトリは必要に応じて作成。 |
| `edit_file` | `file.write` | `replace_all: true` でない限り `old_string` は一意でなければならない。 |
| `delete_file` | `file.write` | |
| `glob_files` | `file.read` | `path` のデフォルトは `.`。 |
| `grep_files` | `file.read` | `glob` で検索対象ファイルを絞り込む。 |

Permission スコープは op の種類ごとに設定されます。`reference/config/permissions.md` を参照してください。

### 粗粒度 `file` 実行バックエンド（Phase からは発行不可）

上記の細粒度 kind が、Phase が LLM に提示し（また LLM から受け付ける）唯一のファイル op です。これらは統一 ToolRegistry を通じてディスパッチされ、内部で粗粒度の `FileIROp`（`{kind: "file", op: ...}`）を構築して共有バックエンド `op_runtime/file.py` にルーティングします。その粗粒度 `file` kind は — `OP_KIND_MODEL_MAP` から削除済み — **LLM が発行できる Control IR kind ではありません**。次の用途でのみ存続します:

- 細粒度ハンドラが委譲する共有実行バックエンド、および
- OS 決定論的な preprocessor `run_op` ステップ（`{kind: file, op: ...}`）、chat ホストのファイルメソッド、`reyn memory` CLI のディスパッチ先。

これらの非 Phase 呼び出し元は、細粒度 kind が公開しない拡張サブ操作 — `mkdir`、`move`、`stat`、`regenerate_index`（`reyn memory` やインデックスを管理するスキルが preprocessor / CLI 経由で使用、Phase Control IR としては決して使われない）— にも到達します。

## `ask_user`

Phase を一時停止してユーザーに質問します。OS は質問を表示し、stdin を読み取り、回答を `user_message` artifact として入力にマージした上で**同じ Phase** を再実行します。訪問カウントは増加しません。

```json
{
  "kind": "ask_user",
  "question": "どのモデルをターゲットにしますか？",
  "suggestions": ["light", "standard", "strong"]
}
```

## `present`

バルクデータと宣言的な view を、LLM の出力トークンを介さずにユーザー向けサーフェスへ直接ルーティングします。オフロードされた ref ファイルはすでに「データファイル + ハンドル」であり、`present` はそのハンドルを view に結び付けて、バルクバイトを直接ユーザーへ届けます。N 行を提示するコストは出力トークン ~0 — エージェントがデータを *変換* する必要が生じた瞬間にだけ ref を読むコストを払います。

**Tier 0**(`ask_user` の兄弟): ユーザー(信頼のルート)への提示は exfiltration チャネルではないため、出力側の permission ゲートはありません。唯一のゲート: `data_ref` の read authority は `file.read` と**まったく同一に**解決されます — `present` はエージェントの file op が読めるより多くを読むことは決してできません。`ask_user` と異なり `present` は **fire-and-continue** です — run を一時停止しません。

```json
{
  "kind": "present",
  "data_ref": ".reyn/cache/tool-results/2026-.../structured.json",
  "blueprint": {
    "component": "table",
    "rows": {"$bind": "/results"},
    "columns": [
      {"header": "Title", "path": "/title"},
      {"header": "Author", "path": "/author"}
    ]
  }
}
```

フィールド(ソースは正確に1つ; `view` / `blueprint` は最大1つ——両方省略も有効、後述の PR-1 の注記を参照):

- `data_ref`(str) **XOR** `data_inline`(any) — データソース。`data_ref` は zone-readable な任意のパスで、オフロードされた `structured_ref` は(LLM 可視のプレビューからではなく)`file.read` セマンティクスで**フルの値に再水和**されます。`data_inline` は既に LLM のコンテキストにある小さなデータです。
- `view`(str) `blueprint`(object | array) と**最大1つ** — view。`view` は登録済みのプレゼンテーション名(registry + fallback chain)、`blueprint` はインラインの宣言的コンポーネントツリーです。(FP-0055 PR-1 でこの引数を `template` から改名——クリーンブレイクでエイリアスなし。語彙の分割: `view` は宣言的な意味、`template` は `render_template` op の Jinja2 テキストテンプレート専用として予約されます。)
- **両方省略(FP-0055 PR-1)**: 有効——「明示的な view なし」として下記の stage-3/4 デフォルトビューア合成へ直接進みます; `present(data_ref=...)` 単独で「そのまま見せる」動作になります。

**宣言的モデル(v1 カタログ — display-only、構造的に非実行)。** blueprint は単一のコンポーネントノードか、そのリスト(上から下へレンダリング)です。カタログコンポーネント(すべて read-only): `text` / `markdown` / `code` / `diff` / `keyvalue` / `table` / `list` / `image`。v1 には**インタラクティブなコンポーネントはありません**(ボタン / フォーム無し)。バインディングは構造的に `{"$bind": "<json-pointer>"}` として表現されます — RFC 6901 JSON Pointer **文字列**(`""` = ドキュメント全体)。それ以外はすべてリテラルです。`table` / `list` の column path は**行相対**(反復される各行に対して相対的)に解決されます。op validation 時の構造ゲートは非カタログコンポーネントや非パスバインディングを拒否します(ソフトドロップではなくハードエラー) — これは純粋に構造的なものであり、leaf-string の無害化は(下記の)レンダー層の単一シームであって parse 時のものではありません。

**バインディングセマンティクス。** パスヒット → バインド。パスミス → そのバインディングを**ソフトスキップ**して `bindings_dropped` に記録(ハード失敗にはならない)。型不一致 → 強制変換(`table` の `rows` スロットにスカラーが入る → 1行のテーブル)+ 記録。Guard による除去 → presentation-guard によって無害化またはサイズキャップされたバインド済み leaf が記録されます。**すべての**バインディングがミスした場合、op は `all_bindings_missed` を報告します(汎用ビューアへのフォールバックシグナル)。

**Presentation-guard(出力シーム)。** 一度も ingest されていないデータを含め、**無条件に**実行されます。レンダーされる leaf 文字列 — ラベル、リテラルスロット値、およびバインドされたデータ値 — はすべて、対象**サーフェス**によって選択される単一のニュートラライザーを通過します(サーフェスごとの戦略なので、将来の web サーフェスも binding 層に触れずに差し込めます)。v1 の**terminal** 戦略は ESC / 制御シーケンス(OSC / CSI)のみをストリップし、Rich コンソールマークアップのエスケープや HTML エスケープは**行いません**。Rich マークアップの安全性は意図的にこのシームの責務ではありません(PR-B での見直し): Rich console-markup インジェクションは `console.print(str, markup=True)` を通じてのみ到達可能 — これは terminal sink の性質ではなく *renderer* が Rich オブジェクトごとに行う選択です。inline-CUI レンダラーはすべての leaf を markup-inert な Rich オブジェクト(`Text` / `Syntax` / `Markdown`)に流し込み、提示されたコンテンツに対して markup 解釈付きで `console.print` を呼び出すことは決してないため、guard の挙動にかかわらず Rich インジェクションは構造的に不可能です — guard 自身の ESC-strip と同じ「ポリシーではなく形状による安全性」という規律です。HTML の無害化は将来の web レンダラー自身の関心事のままです(terminal では `<div>` は無害なリテラルであり、entity-escaping は `code` / `diff` コンテンツを壊してしまいます)。**バインディング単位のサイズキャップ**は、`/`(root)ポインタが `text` コンポーネントにバインドされてファイル全体をダンプするのを防ぎます。無害化は変換です(値はレンダリングされ続けますが無害) — ref はフル忠実度のソースであり続けます。

**Ack(op 結果)** — LLM への唯一のフィードバックで、意図的にコンパクト・高シグナルです:

```yaml
ok: true
mode: view        # view | blueprint | default (FP-0055 PR-1) — 呼び出し側がどの入力を与えたか
bindings_resolved: 3
rows: 500
bindings_dropped:
  - {path: "/results/0/author", reason: path_not_found}
  # reason ∈ {path_not_found, type_mismatch, guard_stripped}
```

`path_not_found` が多くの行にわたる場合は「view がこのデータ形状に一致していない」と読め、`type_mismatch` は「パスは合っているがコンポーネントが違う」、`guard_stripped` は「view のバグではなく guard によってコンテンツが無害化された」と読めます。LLM はデータを ingest せずに、数十トークンでブラインドな presentation を自己修正できます。`mode: "default"`(`view` も `blueprint` も未指定)の場合、上記の統計は合成されたデフォルトビューア自身のものです——これは意図されたレンダリングなので、そのデフォルトビューア自身がさらに stage-4 ジェネリックフォールバックへ劣化しない限り fallback `note` は付きません。

発行されるイベント: `presented`(P6 audit) — `{data_ref, view, mode, surface, ingested, bindings_resolved, bindings_dropped, rows, fallback_stage}`。`view` は登録名、インライン blueprint では `blueprint:<hash>`、両方未指定の場合は `null` です。`fallback_stage`(`null` | `content_type_default` | `generic`)は実際にユーザーへ届いたビューアを記録します — 要求された描画が直接描画されたときは `null`、そうでなければ合成フォールバックの段階です — これにより、要求どおり描画されたリテラルのみビューを、未知 / 全ミスで引き継がれたフォールバックと区別できます(両者とも `bindings_resolved=0` を共有するため)。`ingested`(`none` | `partial` | `full`)は**OS が計算**します(データがインラインだったか、セッション内でそれより前に ref への `read_file` が現れているか) — LLM の自己申告では決してありません。イベントには**ref と統計のみが含まれ、コンテンツバイトは含まれません**(データはすでに ref 内で永続化されています)。

> PR-B: inline-CUI レンダラーが配線されています(chat セッションの `OpContext.presentation_renderer` が設定されていれば `surface: ["inline-cui"]`、そうでなければ `["null"]` — 例えば presentation_renderer 無しで組み立てられた素の `OpContext` は PR-A の元の挙動のまま)。会話のスクロールバック内でワンショットのインラインブロックとして `ResolvedPresentation.nodes` をレンダリングします(`interfaces/repl/present_renderer.py`、既存の Rich `Console` → `StringIO` → `run_in_terminal()` パターンに乗る形)。明示的な per-render terminal width を使用します(Rich は `StringIO` へ書き込む際に幅を自動検出できないため)。`presentations.yaml` レジストリ + 4段階フォールバックチェーンと replay/rewind 再レンダリングは着地済みです。replay(`reyn events <log>`)時、`presented` イベントはまだ有効な ref からベストエフォートで再レンダリングされるか、ref が失われている場合は audit イベントを指す expiry プレースホルダを表示します — display-only な投影であり(状態の再構築ではない)。全体像は [Concepts: Present layer](../../concepts/runtime/present.ja.md) と [Present op & surface reference](present.ja.md) を参照してください。

## `sandboxed_exec`

宣言された `SandboxPolicy` と OS が選択した `SandboxBackend` を介して `argv` を実行します。分離強制が必要な（または将来必要になる）ケースで `shell` を置き換えます。

```json
{
  "kind": "sandboxed_exec",
  "argv": ["echo", "hello"],
  "network": false,
  "read_paths": ["{{workspace}}"],
  "write_paths": ["{{workspace}}/output"],
  "allow_subprocess": false,
  "env_passthrough": ["PATH"],
  "timeout_seconds": 60
}
```

フィールド:
- `argv`（必須）— コマンドと引数。`argv[0]` が実行可能ファイル。
- `network`（省略可、デフォルト `false`）— アウトバウンドネットワークを許可。
- `read_paths`（省略可）— プロセスが読み取り可能なファイルシステムパス（glob パターン可）。
- `write_paths`（省略可）— プロセスが書き込み可能なファイルシステムパス。
- `allow_subprocess`（省略可、デフォルト `true`）— 子プロセス生成の許可。
- `env_passthrough`（省略可）— 引き渡す環境変数名（それ以外は除去）。
- `timeout_seconds`（省略可、デフォルト `60`）— ウォールクロック上限。

**バックエンド選択**: `get_default_backend()` がプラットフォームに応じて選択します。macOS < 26 では `SeatbeltBackend`（sandbox-exec SBPL）。Linux ≥ 5.13 かつ `sandbox-linux` extra インストール済みの場合は `LandlockBackend`（+ オプションの seccomp-BPF スタック）。その他のプラットフォームまたは選択バックエンドが利用不可の場合は `NoopBackend`（監査のみ、強制なし）にフォールバック — 初回使用時に一行 WARN を出力。`reyn.yaml` の `sandbox.backend`（`auto` | `seatbelt` | `landlock` | `noop`）および `sandbox.on_unsupported`（`warn` | `error` | `ignore`）で上書き可能。

結果フィールド: `returncode`、`stdout`、`stderr`、`truncated`、`backend`。

発行イベント: `sandboxed_exec_started`、`sandboxed_exec_completed`（P6 監査証跡）。

## `web_search`

DuckDuckGo を使って公開ウェブを検索し、構造化された結果を返します。**Tier 1** — デフォルト許可；Permission 宣言不要。`reyn.yaml` の `web.search: deny` でプロジェクト全体をブロックできます。

```json
{
  "kind": "web_search",
  "query": "reyn agent OS site:github.com",
  "max_results": 10,
  "backend": "duckduckgo"
}
```

フィールド: `query`（必須）、`max_results`（省略可、デフォルト `10`）、`backend`（省略可、デフォルト `"duckduckgo"`；現在唯一サポートされる値）。

`query` では標準の DuckDuckGo 検索 operator が使用できます:

- `site:<domain>` — 特定ドメインに絞り込む（例: `site:news.ycombinator.com`）
- `"phrase"` — phrase 完全一致
- `-term` — `term` を含む結果を除外

ユーザーの意図が特定サイトや phrase に限定される場合に operator を使用し、それ以外は通常のキーワードで問題ありません。結果は `results` フィールドの `{title, url, snippet}` オブジェクトのリストとして返されます。

## `web_fetch`

単一 URL を取得し、テキスト抽出したコンテンツを返します。**Tier 1** — デフォルト許可；Permission 宣言不要。通常は `web_search` の後、特定の結果ページを詳しく読むために使用します。`reyn.yaml` の `web.fetch: deny` でブロック、`web.fetch: allow` で明示的に事前承認できます。

```json
{
  "kind": "web_fetch",
  "url": "https://example.com/article",
  "prompt": "主要な知見を抽出する",
  "max_length": 50000
}
```

フィールド: `url`（必須）、`prompt`（省略可 — 何を抽出するかの LLM 向けヒント。OS は実行しない）、`timeout`（省略可、デフォルト `30` 秒）、`max_length`（省略可、デフォルト `50000` 文字）。

HTML レスポンスはテキスト抽出されます（script、style、非コンテンツタグは除去）。コンテンツが `max_length` を超える場合は切り詰められ、結果に `truncated: true` が付きます。非 HTML レスポンスはそのまま返されます。

## `mcp`

設定済み MCP server のツールを呼び出します。`reyn.yaml` の `mcp.servers:` に server が宣言されており、かつ Skill の `permissions.mcp` frontmatter ブロックに列挙されている必要があります。

```json
{
  "kind": "mcp",
  "server": "filesystem",
  "tool": "read_text_file",
  "args": {"path": "README.md"}
}
```

フィールド: `server`（必須 — `reyn.yaml` の `mcp.servers:` のキーと一致する必要がある）、`tool`（必須 — server の `tools/list` レスポンスで公開されているツール名）、`args`（省略可、デフォルト `{}`）。

> **提示名。** Phase はこの op を chat-tool 名 `call_mcp_tool` として LLM に提示し、OS がパース境界で `mcp` kind にエイリアスし直します。`mcp` は `OP_KIND_MODEL_MAP` 上およびディスパッチされる op 上の正規 kind のままです。

OS は server のトランスポート（`stdio`、`http`、`sse`）を解決し、`MCPClient` 経由でディスパッチして、ツール結果を返します。呼び出しごとに `mcp_called`、`mcp_completed`、（失敗時）`mcp_failed` イベントが発行されます。

server の設定、トランスポートの選択、セキュリティモデルについては [concepts/tools-integrations/mcp.md](../../concepts/tools-integrations/mcp.md) を参照してください。

## `mcp_install`

`registry.modelcontextprotocol.io` から MCP server をプロジェクト設定にインストールします。**Phase 専用**（ルーターからは使用不可）。Skill frontmatter に `permissions.mcp_install: true` が必要で、ユーザー承認も必要です。

```json
{
  "kind": "mcp_install",
  "server_id": "io.github.modelcontextprotocol/server-filesystem",
  "scope": "local",
  "env_overrides": {"GITHUB_TOKEN": "ghp_..."}
}
```

フィールド:
- `server_id`（必須）— レジストリ識別子（例: `"io.github.foo/bar-mcp"`）。
- `scope`（省略可、デフォルト `"local"`）— 書き込む設定層:
  - `"local"` → `<project>/.reyn/config.yaml`
  - `"project"` → `<project>/reyn.yaml`
  - `"user"` → `~/.reyn/config.yaml`
- `env_overrides`（省略可）— シークレット環境変数の事前提供値。ここに指定したキーは対話型プロンプトをスキップ。

ハンドラーのライフサイクル:
1. `RegistryClient` で `server.json` を取得
2. ランタイムコマンドの利用可能性確認（`npx` / `uvx` / `docker` / `dnx`）
3. `PermissionResolver.require_file_write`（= `.reyn/config/mcp.yaml`）+ `require_http_get`（= registry host）でゲート。 旧 `require_mcp_install` bool-axis gate は廃止済み
4. `intervention_bus` 経由で `isSecret=true` 環境変数を収集；各 `save_secret` は `PermissionResolver.require_secret_write` を経由（= Phase 6 で wildcard `"*"` が runtime-determined key set を許可）
5. 対象スコープの設定ファイルに `mcp.servers.<name>` を書き込む
6. `mcp_server_installed` イベントを発行（P6）— キー名のみ。値は含まない

> **このセクションは stale です — 現行状態は英語版 [Control IR § mcp_install](control-ir.md#mcp_install) 内のノート、および [`embed`](control-ir.md#embed) / [`index_update`](control-ir.md#index_update) / [`semantic_search`](control-ir.md#semantic_search) の各セクションを参照してください。** 要点: `index_write` op は削除されたままですが、`embed` op は FP-0057 Phase 1 で再導入され（ユーザー向け raw embedding primitive）、FP-0057 Phase 2a で `index_update`（差分 reconcile ingestion）が追加されました。safe-mode の `python` step は今や `reyn.api.safe.index_update()`（`index_update` op への薄いディスパッチ）を呼びます — 旧 `reyn.api.safe.embed_index.embed_and_index()`（provider-direct、append/replace）は FP-0057 Phase 2b で **clean-break で削除**されました(shim なし)。`index_update`（ingestion）と `semantic_search`（旧 `recall`、query）はどちらも embed 呼び出しを共有の `embed` op 経由でディスパッチします(provider-direct ではありません)。`EmbeddingProvider` と `SqliteIndexBackend` のプリミティブは変わっていません。

## `index_query`

インデックス済みソース 1 件に対してセマンティック類似検索を行います。

```json
{
  "kind": "index_query",
  "source": "project_docs",
  "query_vector": [0.1, 0.2, ...],
  "top_k": 5,
  "filters": {"path": "docs/concepts"}
}
```

フィールド:

- `source`（str、必須）— 論理ソース名。
- `query_vector`（list[float]、省略可）— 事前計算済み埋め込み。`null` の場合はカタログ列挙にフォールバック（`fallback_size_cap` トークン上限）。
- `top_k`（int、デフォルト `5`）— 返す結果数。
- `filters`（dict[str, str]、省略可）— ランキング前に適用するメタデータキー/値フィルター。
- `fallback_size_cap`（int、デフォルト `4096`）— `query_vector` が `null` のときの列挙フォールバックのトークン上限。

戻り値: `{"kind": "index_query", "source": str, "results": [{"text": str, "score": float, "metadata": dict}]}`.

## `semantic_search`

マクロ op: クエリを embed → 各ソースに index_query → グローバルにトップ K をマージして結果を返します。RAG 取得において推奨される高レベル op です。**FP-0057 Phase 2a: `recall` から rename**（clean break — 観測された `recall`/`search_actions`/`memory` の命名衝突を解消; compat alias なし）。

```json
{
  "kind": "semantic_search",
  "query": "クラッシュリカバリはどのように動作しますか？",
  "sources": ["project_docs", "api_reference"],
  "top_k": 5,
  "embedding_model": "standard"
}
```

フィールド:

- `query`（str、必須）— embed して検索する自然言語クエリ。
- `sources`（list[str]、必須）— 検索する論理ソース名。空にはできません。
- `top_k`（int、デフォルト `5`）— グローバルマージ後に返す結果数。
- `filters`（dict[str, str]、省略可）— 各 `index_query` サブ op に転送。
- `embedding_model`（str、デフォルト `"standard"`）— `embed` サブ op に転送するモデルクラス。

戻り値: `{"kind": "semantic_search", "results": [{"text": str, "score": float, "source": str, "metadata": dict}]}`.

イベント: モデルグループの embed 呼び出しが失敗した場合に `semantic_search_embed_failed`（query、model、error）。

## `index_drop`

インデックス済みソースを完全に削除します。SQLite バックエンドとマニフェストエントリを削除します。**破壊的かつ不可逆です。** Skill frontmatter に `permissions.index_drop: ask`（または明示的な `allow`）が必要で、デフォルトでユーザー承認ゲートが発動します。

```json
{
  "kind": "index_drop",
  "source": "project_docs"
}
```

フィールド:

- `source`（str、必須）— 削除する論理ソース名。

戻り値: `{"kind": "index_drop", "source": str, "chunks_dropped": int}`.

イベント: `index_dropped`（`source`、`chunks_dropped`）。

---

**コントリビューター向けメモ:** `src/reyn/schemas/models.py` および `src/reyn/core/op_runtime/registry.py` に新しい Control IR op kind を追加する際は、**同じ PR でここにセクションを追加してください**。reference と registry は同期を保つ必要があります。ルールの詳細は [CLAUDE.md](https://github.com/tya5/reyn/blob/main/CLAUDE.md) を参照してください。

## LLM に op が提示される場所

OS は利用可能な op をすべてのコンテキストフレームに `available_control_ops` として注入します。各エントリーは `kind`、一行の説明、動作例を含みます。LLM は意図を説明にマッピングして op を選択します。Phase の Markdown は op の構文を説明してはなりません（P8）。

## 関連情報

- [events.md](events.md) — op の種類ごとに発行されるイベント
- コンセプト: principles P8 (principles doc removed)
