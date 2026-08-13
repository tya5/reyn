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
| `present` | バルクデータと宣言的な view を、LLM の出力トークンを介さずにユーザー向けサーフェスへ直接ルーティングする（fire-and-continue） | Tier 0（常に許可）；`data_ref` の read authority は `file.read` と同一 |
| `render_template` | 構造化データに対して Jinja2 テンプレートをレンダリングし文字列にする（サンドボックス化された producer — 副作用無し、sink 無し） | `template_ref` / `data_ref` の read authority は `file.read` と同一；インラインのみは純粋な計算（ゲート無し） |
| `sandboxed_exec` | `SandboxPolicy` と `SandboxBackend` を介して argv を実行する(削除済みの `shell` op を置き換え) | バックエンドが強制（`SandboxPolicy`） |
| `web_search` | DuckDuckGo で公開ウェブを検索する | Tier 1 — デフォルト許可；`reyn.yaml` の `web.search: deny` でブロック |
| `web_fetch` | 単一 URL を取得してテキストを抽出する | Tier 1 — デフォルト許可；`reyn.yaml` の `web.fetch: deny` でブロック |
| `mcp` | 設定済み MCP server のツールを呼び出す | Skill frontmatter の `permissions.mcp: [server_name]` |
| `mcp_read_resource` | 設定済み MCP server から 1 件の resource（または解決済みの resource-template URI）を読み取る | Skill frontmatter の `permissions.mcp: [server_name]`（`mcp` と同じ軸） |
| `mcp_subscribe_resource` | 1 つの resource URI に対する server-pushed な `resources/updated` 通知を購読する（永続接続が必要 — 後述） | Skill frontmatter の `permissions.mcp: [server_name]`（`mcp` と同じ軸） |
| `mcp_unsubscribe_resource` | 既存の `mcp_subscribe_resource` をキャンセルする | Skill frontmatter の `permissions.mcp: [server_name]`（`mcp` と同じ軸） |
| `mcp_get_prompt` | 設定済み MCP server から名前で 1 件の rendered prompt（messages）を取得する | Skill frontmatter の `permissions.mcp: [server_name]`（`mcp` と同じ軸） |
| `mcp_install` | レジストリから MCP server をプロジェクト設定にインストールする | Skill frontmatter の `permissions.mcp_install: true` |
| `mcp_drop_server` | MCP server をプロジェクト/local/user 設定から削除する（`mcp_install` の逆） | Skill frontmatter の `permissions.mcp_drop_server: true` |
| `skill_install` | Skill（ローカルディレクトリまたは git/URL source）をプロジェクトの skills 設定に登録する | Skill frontmatter の `file.write: [.reyn/config/skills.yaml]`；`source` 設定時は `http.get: [{host: <source_host>}]` |
| `load_skill` | Skill の `SKILL.md` 本体を読み込む — 専用の skill-activation verb（FP-0066 P0、#3247）；登録済み skill の invocation 時 `${REYN_*}`/`${CLAUDE_*}`/`${env:VAR}` トークンを展開する | `file.read` |
| `pipeline_install` | Pipeline（ローカル DSL ファイルまたは git/URL source）をプロジェクトの pipelines 設定に登録する | Skill frontmatter の `file.write: [.reyn/config/pipelines.yaml]`；`source` 設定時は `http.get: [{host: <source_host>}]` |
| `presentation_install` | 名前付き presentation テンプレート（インライン blueprint）をプロジェクトの presentations 設定に登録する | `file.write: [.reyn/config/presentations.yaml]` |
| `embed` | 生の embedding primitive: テキストのバッチ → ベクトル | なし（デフォルト許可; embedding API コスト） |
| `index_query` | インデックス済みソース 1 件に対してセマンティック検索を行う | なし |
| `semantic_search` | マクロ（FP-0057 Phase 2a; `recall` から rename）: embed → 各ソースに index_query → トップ K をマージ | なし |
| `index_drop` | インデックス済みソースを完全削除する（破壊的） | Skill frontmatter の `permissions.index_drop: ask` |
| `index_update` | ソースの index への差分/delta-reconcile ingestion（add/update/remove/skip） | なし（デフォルト許可；own-write；embedding API コスト） |
| `compact` | 会話履歴を任意で今すぐ圧縮する（advisory） | なし（LLM コスト；必須の `retry_loop` backstop とは独立） |
| `emit_hook_event` | LLM が作成する `llm:<session_id>:<event_name>` の hook-event を、呼び出し元自身のセッションの `HookBus` に発行する（Hook-Event Redesign Phase 5 part 2） | なし（構造的な session-binding + 静的な kind ホワイトリストが autonomy boundary をゲート — 下記の専用セクション参照） |

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

**バインディングセマンティクス。** パスヒット → バインド。パスミス → そのバインディングを**ソフトスキップ**して `bindings_dropped` に記録(ハード失敗にはならない)。型不一致 → 強制変換(`table` の `rows` スロットにスカラーが入る → 1行のテーブル; コンテナが `text` スロットに入る → その JSON 形式)+ `{path, rendered_as}` を **`coerced`** に記録(`bindings_dropped` ではない)——型変換は drop の*逆*の結果であり、値は形を変えてユーザーに届いた(届かなかったのではない、#3664)。Guard による除去 → presentation-guard によって無害化またはサイズキャップされたバインド済み leaf は `bindings_dropped` に記録されます。**すべての**バインディングがミスした場合、op は `all_bindings_missed` を報告します(汎用ビューアへのフォールバックシグナル)。

**Presentation-guard(出力シーム)。** 一度も ingest されていないデータを含め、**無条件に**実行されます。レンダーされる leaf 文字列 — ラベル、リテラルスロット値、およびバインドされたデータ値 — はすべて、対象**サーフェス**によって選択される単一のニュートラライザーを通過します(サーフェスごとの戦略なので、将来の web サーフェスも binding 層に触れずに差し込めます)。v1 の**terminal** 戦略は ESC / 制御シーケンス(OSC / CSI)のみをストリップし、Rich コンソールマークアップのエスケープや HTML エスケープは**行いません**。Rich マークアップの安全性は意図的にこのシームの責務ではありません(PR-B での見直し): Rich console-markup インジェクションは `console.print(str, markup=True)` を通じてのみ到達可能 — これは terminal sink の性質ではなく *renderer* が Rich オブジェクトごとに行う選択です。inline-CUI レンダラーはすべての leaf を markup-inert な Rich オブジェクト(`Text` / `Syntax` / `Markdown`)に流し込み、提示されたコンテンツに対して markup 解釈付きで `console.print` を呼び出すことは決してないため、guard の挙動にかかわらず Rich インジェクションは構造的に不可能です — guard 自身の ESC-strip と同じ「ポリシーではなく形状による安全性」という規律です。HTML の無害化は将来の web レンダラー自身の関心事のままです(terminal では `<div>` は無害なリテラルであり、entity-escaping は `code` / `diff` コンテンツを壊してしまいます)。**バインディング単位のサイズキャップ**は、`/`(root)ポインタが `text` コンポーネントにバインドされてファイル全体をダンプするのを防ぎます。無害化は変換です(値はレンダリングされ続けますが無害) — ref はフル忠実度のソースであり続けます。

**Ack(op 結果)** — LLM への唯一のフィードバックで、意図的にコンパクト・高シグナルです:

```yaml
ok: true
mode: view        # view | blueprint | default (FP-0055 PR-1) — 呼び出し側がどの入力を与えたか
bindings_resolved: 3
bindings_dropped:
  - {path: "/results/0/author", reason: path_not_found}
  # reason ∈ {path_not_found, guard_stripped} — 型変換の coercion は drop ではない
  # (#3664); 下記の `coerced` を参照
coerced:
  - {path: "/big", rendered_as: json_text}
  # rendered_as ∈ {json_text, single_row} — 値はユーザーに届いた(形を変えて)
rows: 500          # #3664: すべての行状スロット(table/list と keyvalue)を横断した、
                    # 実際に描画された行数(キャップ後)——LLM がどれだけ届いたかを
                    # 知る唯一の指標
```

`path_not_found` が多くの行にわたる場合は「view がこのデータ形状に一致していない」と読め、`guard_stripped` は「view のバグではなく guard によってコンテンツが無害化された」、`coerced` エントリは「パスは合っているがコンポーネントが違う——drop ではなく形を変えてユーザーに届いた」と読めます。LLM はデータを ingest せずに、数十トークンでブラインドな presentation を自己修正できます。`mode: "default"`(`view` も `blueprint` も未指定)の場合、上記の統計は合成されたデフォルトビューア自身のものです——これは意図されたレンダリングなので、そのデフォルトビューア自身がさらに stage-4 ジェネリックフォールバックへ劣化するか、(#3664)コンテナを JSON テキストへ変換しない限り fallback `note` は付きません(後者は `view`/`blueprint` パスの `all_bindings_missed` と同じ自己修正シグナルを、本来それを持たないこのパスに与えます)。

発行されるイベント: `presented`(P6 audit) — `{data_ref, view, mode, surface, ingested, bindings_resolved, bindings_dropped, coerced, rows, fallback_stage}`。`view` は登録名、インライン blueprint では `blueprint:<hash>`、両方未指定の場合は `null` です。`fallback_stage`(`null` | `content_type_default` | `generic`)は実際にユーザーへ届いたビューアを記録します — 要求された描画が直接描画されたときは `null`、そうでなければ合成フォールバックの段階です — これにより、要求どおり描画されたリテラルのみビューを、未知 / 全ミスで引き継がれたフォールバックと区別できます(両者とも `bindings_resolved=0` を共有するため)。`ingested`(`none` | `partial` | `full`)は**OS が計算**します(データがインラインだったか、セッション内でそれより前に ref への `read_file` が現れているか) — LLM の自己申告では決してありません。イベントには**ref と統計のみが含まれ、コンテンツバイトは含まれません**(データはすでに ref 内で永続化されています)。

> PR-B: inline-CUI レンダラーが配線されています(chat セッションの `OpContext.presentation_renderer` が設定されていれば `surface: ["inline-cui"]`、そうでなければ `["null"]` — 例えば presentation_renderer 無しで組み立てられた素の `OpContext` は PR-A の元の挙動のまま)。会話のスクロールバック内でワンショットのインラインブロックとして `ResolvedPresentation.nodes` をレンダリングします(`interfaces/repl/present_renderer.py`、既存の Rich `Console` → `StringIO` → `run_in_terminal()` パターンに乗る形)。明示的な per-render terminal width を使用します(Rich は `StringIO` へ書き込む際に幅を自動検出できないため)。`presentations.yaml` レジストリ + 4段階フォールバックチェーンと replay/rewind 再レンダリングは着地済みです。replay(`reyn events <log>`)時、`presented` イベントはまだ有効な ref からベストエフォートで再レンダリングされるか、ref が失われている場合は audit イベントを指す expiry プレースホルダを表示します — display-only な投影であり(状態の再構築ではない)。全体像は [Concepts: Present layer](../../concepts/runtime/present.ja.md) と [Present op & surface reference](present.ja.md) を参照してください。

## `render_template`

構造化データに対して Jinja2 テンプレートをレンダリングし、プレーンな文字列にします。汎用の、サンドボックス化された**producer**です: `data + template → string`、**副作用無し、sink 無し**（レンダリングされた文字列は通常の op 結果として返されます — canonical `text`；大きな出力は chat パスで自動オフロードされます）。呼び出し元はそれを任意の sink へルーティングします: `present`、`write_file`、メッセージ本文、または pipeline の `ctx`。

構造化データをユーザーに見せるには（宣言的な）`present` を優先してください — トークン経済的でポータブルです。`render_template` が必要なのは**計算されたテキスト**が要る場合のみです: ループ / 条件分岐 / 集約が散文に織り込まれる形は、宣言的なバインディングでは意図的に表現できません。

```json
{
  "kind": "render_template",
  "template": "{% for r in data.results %}- {{ r.title }}\n{% endfor %}",
  "data_ref": "runs/summary.json",
  "undefined": "strict"
}
```

フィールド:
- `template`（`template_ref` と XOR）— インラインの Jinja2 ソース文字列。
- `template_ref`（`template` と XOR）— zone-readable なテンプレートファイルパス。`file.read` authority で**生テキスト**として読まれます（テンプレートファイルはソーステキストであり、JSON 再水和はされません）。
- `data_ref`（`data_inline` と XOR）— zone-readable な任意のパス。`file.read` セマンティクス（`present` が使うのと同じシーム）でフルの値に再水和されます。
- `data_inline`（`data_ref` と XOR）— 既に LLM のコンテキストにある小さなオブジェクト。
- `undefined`（省略可、デフォルト `"strict"`）— `"strict"`: 未定義の変数は**欠けている名前を名指しするハードエラー**（デフォルトで loud なので、file sink が黙って壊れた artifact を書くことはない）；`"lenient"`: 未定義は空にレンダリングされ、参照されたが未バインドの名前は結果メタの `undefined_vars` に表れます。

解決されたデータはテンプレートコンテキストの **`data`** 配下にバインドされます（`{{ data.results[0].title }}`）。

**サンドボックスと無害性。** エンジンは常に `jinja2.sandbox.SandboxedEnvironment`（唯一のファクトリ `reyn.security.template_env.make_sandboxed_env` 経由）です — テンプレートは LLM が作成する場合があり、サンドボックス化されていない Jinja2 は任意コード実行（SSTI）です。ブロックされた属性トラバーサル（`{{ ().__class__ }}`）は sandbox violation を raise し、構造化された `error` 結果になります；何も実行されません。`autoescape` は**OFF**です: op は RAW なレンダリング済みバイトを返します。無害化は**sink**の責務です（terminal はガードで制御バイトを除去、file は無害、web サーフェスは HTML エスケープ）— producer 側でのエスケープは file / terminal の artifact を壊してしまいます。

**Read-authority の等価性。** `template_ref` / `data_ref` はまさに `file.read` ゲートを通して解決されます；拒否された read は `status="denied"`。render_template は agent の `file.read` が読める以上のものを読むことは決してできません。インラインのみの呼び出し（`template` + `data_inline`）は純粋な計算です — read ゲート無し。

**リソース bound。** `SandboxedEnvironment` は SSTI を止めますが、リソース枯渇は止めません — `{% for i in range(10**9) %}` のような bound の無いループはまだ溢れます。cap は生成**中**に適用されます（`template.generate(context)` のストリーミング）、max-output-chars バジェットに対してウォールクロック backstop 付きで累積します；どちらかを超えた瞬間にレンダリングは停止し、結果は発火した bound を名指しする `truncated: true` メタフラグとともに TRUNCATED されます（`truncate_reason`）— bound された結果であり、OOM やハングにはなりません。bound はデフォルトで safety-spirit 定数です（`OpContext.render_template_bounds` で operator が調整可能）。

結果フィールド: `rendered`（文字列）、`truncated`、`truncate_reason`（truncated 時）、`undefined_vars`（lenient モード）。エラー結果は `status="error"` + `error_kind`（`template_error` | `security` | `undefined`）+ `error` を持ちます。新しいイベント種別は無し — 標準の op イベントです；(template, data) の純粋な関数なので、通常の memo/replay がそのまま適用されます。

## `sandboxed_exec`

Control IR の op kind は `sandboxed_exec` のまま変わりません（`OP_KIND_MODEL_MAP["sandboxed_exec"]` / `SandboxedExecIROp`）。この op に到達する router/phase ツールは `sandboxed_exec` から **`exec`** へ改名されました（#3226 Phase 3、catalog qualified name は **`exec`**）— この改名は tool/qualified-name のみで、この op のスキーマ・イベント・結果形状には影響しません。op kind と tool 名はそれ以来異なります。2つの文字列を橋渡しするテーブル（`op_runtime.contextual_gate._OP_KIND_TOOLS`）がかつて存在しましたが、#3513 で削除されました — 唯一の consumer だった `control_ir_executor` / `preprocessor_executor` が #2434 でファイルごと削除され、`src/` 内に呼び出し元が無くなっていたためです。op dispatch が独自の contextual narrowing を要するかどうかは #3546 で追跡中で、ここでは未解決です。

宣言された `SandboxPolicy` と OS が選択した `SandboxBackend` を介して `argv` を実行します。分離強制が必要な（または将来必要になる）ケースで `shell` を置き換えます。

```json
{
  "kind": "sandboxed_exec",
  "argv": ["echo", "hello"],
  "stdin": null,
  "timeout_seconds": null
}
```

フィールド:
- `argv`（必須）— コマンドと引数。`argv[0]` が実行可能ファイル。
- `stdin`（省略可、デフォルト `None`）— プロセスの stdin に書き込むバイト列（pipeline `tool` step は前ステップの pipe-data を `args: {argv: [...], stdin_pipe: !expr pipe}` 経由で JSON としてここに渡せる — [Pipeline DSL](pipeline-dsl.ja.md#tool) 参照）。
- `timeout_seconds`（省略可、デフォルト `None`）— **#3903①（2026-08-11、下の段落からの意図的な方針転換）**: LLM は `SandboxPolicy.timeout_seconds` 自身のデフォルトを超える前景ウォールクロックタイムアウトを、`SandboxPolicy.max_timeout_seconds`（operator 自身が設定する上限 — ハードコード値ではない）まで要求できます（`max_timeout_seconds` をデフォルトの 600 秒より狭めた operator にはその上限が実際に強制される。LLM が operator 自身の狭い設定を広げることはできない）。`None`（デフォルト）は policy 自身の `timeout_seconds` を使う。上限を超える値は**拒否**され（`status: "error"`、実際に設定された上限を名指し）、静かに切り詰められることはない — それをすると、下の段落で閉じたはずの「advertised だが無視される」形が、フィールドが黙って落とされる代わりに値が黙って変わる形で再来してしまう。非正の値も同様に拒否される。

**他の policy フィールドは無い**（`network` / `read_paths` / `write_paths` / `allow_subprocess` / `env_passthrough` — #3907 で削除）: 実際に run を統制する sandbox policy の他の軸は、この op からは**一切**設定できません。agent レベル（operator）の `sandbox.policy`（`reyn.yaml`、`resolve_sandbox_policy` 経由で解決 — [`sandbox` config block](../config/reyn-yaml.ja.md#sandbox-ブロック) 参照）、それが無ければ operator の compat/strict デフォルトが使われます — いずれにせよ LLM には見えず広げられない値です（#1326/#1339: operator-or-default policy は op が要求するどんな値にも常に勝つ）。この op はかつて、operator policy が解決されなかった場合のフォールバック source として 5 つの policy フィールドを持っていましたが、#3907① の実測でそのパスは production で到達不能（すべての context-building path が具体的な policy を解決する）と判明したため、advertised-but-ignored な knob として残すのではなく削除されました。

`timeout_seconds` もかつて同じ道をたどりました（#3962 で同じ defect class として削除 — ただし 1 issue 遅れて。ウォールクロック上限は permission 軸ではないため #3907 の一掃を生き延び、1 issue 分長く dead のまま残った）が、上の 5 つとは**異なる軸**です: boundedness であって permission ではない（#3903 自身の framing）。今回は本物の reader を伴って戻ってきました（上記参照） — これが、この方針転換を #3962 が閉じたギャップの再開と区別する点です。

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
  "prompt": "主要な知見を抽出する"
}
```

フィールド: `url`（必須）、`prompt`（省略可 — 何を抽出するかの LLM 向けヒント。OS は実行しない）、`timeout`（省略可、デフォルト `30` 秒）。

HTML レスポンスはテキスト抽出されます（script、style、非コンテンツタグは除去）。非 HTML レスポンスはそのまま返されます。**抽出したテキストはそのまま全量返されます — `web_fetch` 自身のサイズ上限はありません**（#3580: `max_length` 引数と `truncated`/`next_start`/`start_index` のページングフィールドを撤去。`start_index` はツールスキーマに一度も露出しておらず、LLM はページングできませんでした）。別の上限は 2 つ残ります: `reyn.yaml` の `web_fetch.max_download_bytes`（#4174 T4、`web.fetch.max_download_bytes` から改名）がダウンロードする HTTP ボディを制限し（デフォルト 10 MiB、超過は `status: "too_large"`）、結果のどれだけがモデルの文脈に入るかは OS レベルの tool-result cap（`offload.enabled`、デフォルト `false` = 無制限）が決めます — この op ではありません。

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

## `mcp_read_resource`

設定済み MCP server から 1 件の resource（または解決済みの resource-template URI）を読み取ります。#2597 slice ②a（resources 消費）— `mcp`（call_tool）と**同じ** `permissions.mcp` 軸でゲートされます: resource read は外部の、潜在的にセンシティブな server 発の content を返すため、tool call と同一の permission ゲートがかかります。

```json
{
  "kind": "mcp_read_resource",
  "server": "filesystem",
  "uri": "file:///README.md"
}
```

フィールド: `server`（必須 — `reyn.yaml` の `mcp.servers:` のキーと一致する必要がある）、`uri`（必須 — server の `resources/list` で公開されている resource URI、または解決済みの `resources/templates/list` テンプレート）。

> **提示名。** Phase はこの op を chat-tool 名 `read_mcp_resource` として LLM に提示し、OS がパース境界で `mcp_read_resource` kind にエイリアスし直します — `mcp`/`call_mcp_tool` と同じパターンです。

OS は server のトランスポートを解決し、`MCPClient.read_resource`（server が negotiate した `resources` capability でゲート — `mcp/client.py` の `require_capability` 参照）経由でディスパッチして `{"contents": [...]}` を返します。呼び出しごとに `mcp_resource_read`、`mcp_resource_read_completed`、（失敗時）`mcp_resource_read_failed` イベントが発行されます。

**Discovery はゲートされません。** `list_mcp_resources` / `list_mcp_resource_templates`（`MCPClient.list_resources` / `list_resource_templates` の chat-tool 名）は `list_mcp_tools` をミラーします: `control-ir` op kind も permission ゲートも無い純粋な discovery で、router host adapter から `MCPGateway` へ直接ルーティングされます。content を返す read のみがゲートされた op kind であり、既存の `mcp`（call_tool）vs. discovery（`list_tools`）の分割と一致します。

`resources/subscribe` + `resources/updated` push 通知は下記の `mcp_subscribe_resource` / `mcp_unsubscribe_resource`（#2597 slice ②b）です。

## `mcp_subscribe_resource` / `mcp_unsubscribe_resource`

設定済み MCP server 上の 1 つの resource URI に対して、server-pushed な `notifications/resources/updated` を購読（または既存の購読をキャンセル）します。#2597 slice ②b — 非同期の push イベントソース: MCP の `resources/subscribe` は**state-sync/watch** メカニズムであり、メッセージキューではありません — server は薄い「この URI が変わった」シグナル（payload 無し）を push し、OS が（`mcp_read_resource` / `read_mcp_resource` で）再読み取りして新しい content を確認します。

```json
{"kind": "mcp_subscribe_resource", "server": "filesystem", "uri": "file:///README.md"}
```

```json
{"kind": "mcp_unsubscribe_resource", "server": "filesystem", "uri": "file:///README.md"}
```

フィールド（両 kind とも）: `server`（必須）、`uri`（必須 — `resources/list` で公開されている resource URI）。

`mcp` / `mcp_read_resource` と**同じ** `permissions.mcp` 軸でゲートされます（購読は server に対するステートフルなアクションです）。加えて、server が negotiate した `resources.subscribe` サブ capability でもゲートされます — `mcp_read_resource` がゲートする、より粗い `resources` capability とは別物です: server は resource の読み取りをサポートしつつ、その購読はサポートしない場合があります（`MCPClient.subscribe_resource` は、server が接続時に `resources.subscribe=True` を advertise していなければ `MCPCapabilityError` で fail fast します）。

**永続接続が必要。** 購読は HELD（セッション寿命の）MCP 接続の上でのみ意味を持ちます — 購読中の URI 集合は `MCPConnectionService` 上でメモリ内追跡されます（runtime-only、WAL 無し: 購読自体はデータを持たないため完全に再確立可能で、gen-store の runtime-only-state 不変条件と一致します）。エフェメラルなセッション（op が返った直後に per-call `MCPClientPool` が接続を閉じる）は、push を決して観測できない購読を静かに受け入れるのではなく、両 op を明確なエラーで拒否します。

**再接続時は自動的に再購読されます。** トランスポート断による再接続（`mcp`/`mcp_read_resource` が使うのと同じ F1 healing パス）は、購読を一切持たない新しい `mcp.Client`（#3698 PR-1 — 以前は生の `mcp.ClientSession` でした。`Client` は今やその session の構築・enter を内部で自ら行います）を開きます — `MCPConnectionService` は、新しい接続が開いた直後に、その server に対してまだ追跡されている全 URI について `subscribe_resource` を再発行するため、購読はトランスポート断を透過的に生き延びます。

**push 通知自体は `control_ir_results` の値ではなく EventLog イベントです。** server が `notifications/resources/updated {uri}` を送ると、`reyn.mcp.message_handler.ReynMCPMessageHandler.on_resource_updated` がセッションの `EventLog` に `mcp_resource_updated` イベント（`server`、`uri`）を、どの op 呼び出しとも独立に非同期発行します。この slice は意図的に EventLog で止まります: `mcp_resource_updated` を hook dispatcher に配線するのは後続の（hooks-arc の）slice です。切断中に見逃した更新を再接続時に再読み取りして拾う（上記の再**購読**とは別の resync-READ）ことも、この slice ではなく follow-up です。

chat-tool 名 `subscribe_mcp_resource` / `unsubscribe_mcp_resource` として LLM に提示されます — `mcp`/`call_mcp_tool` と同じエイリアスパターンです。

## `mcp_get_prompt`

設定済み MCP server から 1 件の rendered prompt（その messages）を取得します。#2597 slice ②c（prompts 消費）— `mcp`（call_tool）/ `mcp_read_resource` と**同じ** `permissions.mcp` 軸でゲートされます: rendered prompt は外部の、潜在的にセンシティブな server 発の content を返すため、同一の permission ゲートがかかります。

```json
{
  "kind": "mcp_get_prompt",
  "server": "filesystem",
  "name": "summarize",
  "arguments": {"style": "brief"}
}
```

フィールド: `server`（必須 — `reyn.yaml` の `mcp.servers:` のキーと一致する必要がある）、`name`（必須 — server の `prompts/list` レスポンスで公開されている prompt 名）、`arguments`（省略可、デフォルト `{}` — prompt が宣言する `arguments` スキーマに一致する rendering 引数）。

> **提示名。** Phase はこの op を chat-tool 名 `get_mcp_prompt` として LLM に提示し、OS がパース境界で `mcp_get_prompt` kind にエイリアスし直します — `mcp`/`call_mcp_tool` および `mcp_read_resource`/`read_mcp_resource` と同じパターンです。

OS は server のトランスポートを解決し、`MCPClient.get_prompt`（server が negotiate した `prompts` capability でゲート — `mcp/client.py` の `require_capability` 参照）経由でディスパッチして `{"description": str | None, "messages": [...]}` を返します — 各 message はフラット化された `PromptMessage`（`role` + `content`）です。呼び出しごとに `mcp_prompt_get`、`mcp_prompt_get_completed`、（失敗時）`mcp_prompt_get_failed` イベントが発行されます。

**Discovery はゲートされません。** `list_mcp_prompts`（`MCPClient.list_prompts` の chat-tool 名）は `list_mcp_resources`/`list_mcp_tools` をミラーします: `control-ir` op kind も permission ゲートも無い純粋な discovery で、router host adapter から `MCPGateway` へ直接ルーティングされます。content を返す get のみがゲートされた op kind であり、既存の `mcp`/`mcp_read_resource` vs. discovery の分割と一致します。

**Prompt には subscribe の概念がありません。** resource（`mcp_subscribe_resource`/`mcp_unsubscribe_resource`）とは異なり、MCP の `prompts` capability には特定の prompt の content 変更に対する server-push 通知が無く、より粗い `notifications/prompts/list_changed`（`reyn.mcp.message_handler.ReynMCPMessageHandler.on_prompt_list_changed` が、この op kind とは独立に EventLog イベントへブリッジ）のみです。`mcp_subscribe_prompt` は存在しません。

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

## `mcp_drop_server`

MCP server をプロジェクト/local/user 設定から削除します — `mcp_install` の counter-op です（FP-0034 §D23）。純粋に機械的（LLM の推論は不要）で、universal catalog では `mcp.operation__drop_server` に属します。Skill frontmatter に `permissions.mcp_drop_server: true` が必要です — `mcp_install` とは**別の** decl フィールドなので、install intent だけでは drop intent を含意しません（install のみ許可された agent が誤ってユーザー設定済みの server を壊すのを防ぎます）。

```json
{
  "kind": "mcp_drop_server",
  "server": "filesystem",
  "clear_secrets": true
}
```

フィールド:
- `server`（必須）— 短い設定キー（例: `"filesystem"`）。
- `scope`（省略可、デフォルト `None` = 自動検出）— どの設定層から削除するか（`"local"` / `"project"` / `"user"`、`mcp_install` の `scope` と同じマッピング）。省略時は dynamic → local → project → user の順に走査し、`server` を含む最初の層から削除します。
- `clear_secrets`（省略可、デフォルト `true`）— server の `${KEY}=value` シークレットエントリも `~/.reyn/secrets.env` から削除します（削除時点のエントリの `env` ブロックでキー付け）。CLI 自身のデフォルトは安全のためシークレットを残しますが、LLM 側のパスは「LLM の drop intent はより意図的である」という前提でデフォルトでクリーンアップします。

ハンドラーのライフサイクル:
1. scope を解決 — 明示的（`op.scope`）または自動検出で `server` を含む最初の層を探す；どこにも見つからない場合は例外ではなく構造化された `{status: "not_found"}` 結果を返す（LLM は `list_actions`/リトライできる。ターンをクラッシュさせない）。
2. scope の設定ファイルに対して `PermissionResolver.require_file_write` でゲート — `mcp_install` のゲートを反映。旧来の per-server bool-axis `require_mcp_drop_server` プロンプトは #571 permission-collapse arc により廃止済み（per-server の粒度は operator の設定レベルの関心事になり、per-op のランタイム関心事ではなくなった）。
3. エントリの `env` ブロックのキー名を（変更前に）キャプチャし、ステップ 5 のシークレットクリーンアップで使用。
4. scope の YAML からエントリを削除し、空になった `mcp.servers`/`mcp` コンテナを整理；`record_config_generation`（recovery-core: 切り詰め耐性スナップショット、#2259 / CLAUDE.md gate）。
5. `clear_secrets` が `true` の場合、`reyn.security.secrets.store.clear_secret` 経由でキャプチャしたキーを削除 — シークレットの**値**は読まれることも発行されることもなく、キー名のみ。
6. `mcp_server_removed` イベントを発行（P6 監査証跡）。

結果フィールド: `status`（`"ok"` / `"not_found"`）、`server`、`scope`、`removed_path`、`env_keys_cleared`、`secrets_cleared`。

## `skill_install`

Skill（ローカルディレクトリまたは git/GitHub source URL から）をプロジェクトの `skills.entries` 設定に登録します。2 つの tool surface verb が同じ `op_runtime/skill_install.py` ハンドラーに収束します: `skill_install_local`（ローカルパス）と `skill_install_source`（git/URL、PR-D、#2548）。

ローカルパスの例:
```json
{
  "kind": "skill_install",
  "path": "skills/my-skill",
  "name": "my-skill"
}
```

Source/git の例:
```json
{
  "kind": "skill_install",
  "source": "https://github.com/user/skill-repo",
  "name": "my-skill"
}
```

サブディレクトリ規約（Terraform を反映）: `"https://github.com/user/repo//skills/my-skill"` はクローンされたリポジトリ内の `skills/my-skill` サブディレクトリを選択します。

フィールド:
- `path`（`source` が無い場合は必須）— skill ディレクトリ（`SKILL.md` を含む）へのパス、または `SKILL.md` ファイルへの直接パス。絶対パスまたはプロジェクトルート相対パスが可能です。ディレクトリを指す場合、ハンドラーは `/SKILL.md` を追加します。`source` が設定されている場合は無視されます。
- `source`（省略可、PR-D）— git または GitHub URL。ハンドラーはリポジトリを `.reyn/skills/<name>/` へシャロークローンします。リポジトリ内のサブディレクトリは `//` セパレータで指定します。呼び出し元の permission 宣言に `http.get: [{host: <source_host>}]` が必要です。
- `scope`（省略可、デフォルト `".reyn/config/skills.yaml"`）— 後方互換のため保持；現在未使用（すべての install は `.reyn/config/skills.yaml` に書き込みます）。
- `name`（省略可）— 設定キーの上書き。省略時、ハンドラーは以下の順で解決します: frontmatter の `name:` フィールド → ディレクトリのベース名 → リポジトリ/サブディレクトリのベース名。解決された名前は**安全な単一パスコンポーネントに sanitize されます**（`[A-Za-z0-9._-]`；`/`、`\`、`..`、先頭 `.` は不可）— 安全でない名前（呼び出し元の `op.name` または第三者の SKILL.md frontmatter から）は `status="error"` で**拒否**され、パス構築に決して使われません。
- `plugin_id`（省略可、ADR 0064 §3.7、plugin model P2）— 設定されると、書き込まれる `skills.yaml` エントリに `entry["plugin_id"]` としてそのまま刻印されます。`plugin_install` が plugin の `skills/` capability を登録するためこのハンドラーを内部的に呼ぶ際にのみ設定されます — この追加的な provenance フィールドは `plugin_uninstall` が特定の plugin が作成したすべてのエントリを見つけるために読み戻します。直接の `skill_install` 呼び出しでは不在（`None`）で、このフィールドが存在する前と変わりません。

ハンドラーのライフサイクル（source パスはステップ 1 の前に 0a〜0d を挿入）:
0. **Source パスのみ**: (a) source host に対して `require_http_get` でゲート。(b) 候補名を sanitize（`_safe_skill_name`）し、クローン先が `.reyn/skills/` 配下に含まれることを検証（`_contained_under`）— どちらかが失敗した場合、ファイルシステムの変更前に拒否します（path-traversal → arbitrary-rmtree ガード）。リポジトリを `.reyn/skills/<candidate_name>/` へシャロークローン。(c) root またはサブディレクトリで `SKILL.md` を探す。(d) frontmatter の名前が解決され sanitize された後、containment チェック + 名前が candidate と異なる場合はクローンディレクトリをリネーム。
1. `SKILL.md` パスを解決（ディレクトリ → `<dir>/SKILL.md`、または直接ファイル）
2. `SKILL.md` を読んで `split_frontmatter()` — `name` と `description` を抽出
3. 設定されていれば `op.name` の override を適用
4. `content_guard.scan_for_threats(scope="strict")` で description を threat-scan — blocking-severity のマッチでブロック（source パス: ブロック時はクローンを削除）
5. `PermissionResolver.require_file_write`（= `.reyn/config/skills.yaml`）でゲート
6. `.reyn/config/skills.yaml` に `skills.entries.<name>` を `{path, description, enabled: true, visibility: menu}` で書き込み（+ 設定されていれば `source: <url>`）
7. `record_config_generation` を呼ぶ（recovery-core: 切り詰め耐性スナップショット、#2259 / CLAUDE.md gate）
8. `skill_installed` イベントを発行（P6 監査証跡）
9. `get_active_hot_reloader().request_reload(source="skill_install")` 経由で hot-reload をリクエスト

結果フィールド: `status`（`"installed"` / `"blocked"` / `"error"`）、`name`、`path`、`description`、`config_path`、`source`（ローカル install の場合は空文字列）。

発行されるイベント: `skill_install_threat_match`、`skill_install_threat_blocked`（threat scan）、`skill_installed`（成功時 P6）。

## `load_skill`

**FP-0066 P0（#3247）。** 専用の skill-activation verb です — skill の `SKILL.md` 本体を読み込むこと自体が invocation です（#2971 の根拠は今も成立: skill body はモデルへの指示であり、実行されるコードではない；`run_skill` op は今も存在しません）。`read_file` の旧 `is_skill_body_path` 特殊ケースから抽出されました（ADR 0064 §3.5 は元々専用 verb を求めていましたが、#2971 は代わりに通常の read op に折り込みました — これはその drift を逆転させます）。Tool surface: `load_skill`（#3223 naming-convention arc により正式化）。

```json
{
  "kind": "load_skill",
  "path": "skills/my-skill/SKILL.md"
}
```

フィールド:
- `path`（必須）— skill の `SKILL.md` パス。L1 の `## Skills` メニューまたは `skill_list` ツールが提示するもの。

ハンドラーのライフサイクル（`op_runtime/load_skill.py`）:
1. **一度だけ解決（#3196 co-vet round 2、セキュリティクリティカル）**: `op.path` は `reyn.core.op_runtime.context.resolve_path_for_gate` を通じて**厳密に一度だけ**解決され、`resolved_path` になります；以降のすべての判断（permission ゲート、provenance 分類、実際のバイト読み取り）は THIS SAME 文字列を再利用します。「これは信頼できるか」と「何を読むか」を別々に、独立に解決すると、#3196 が閉じた symlink-swap TOCTOU ウィンドウが再び開いてしまいます。
2. **Builtin/plugin バイパス**: `read_builtin_body_bytes`（#2913/#2914）/ `read_plugin_body_bytes`（`plugin_install.is_registered_plugin_root`）— `file.py` が自身の（無関係な）汎用 builtin/plugin body 読み取りで使うのと SAME ヘルパーです。`None` でない結果は既にそれ自体が trusted な provenance（`"builtin"` / `"plugin"`）であり、単なる permission-bypass シグナルではなく、通常の `require_file_read` ゲートをスキップします。
3. **Permission ゲート**: バイパスが発火しなかった場合、`resolved_path` に対して `require_file_read`。
4. **読み取り + デコード**: `ctx.workspace.read_file_bytes`（またはバイパスのバイト列）→ 共有のテキストコーデックデコードラダー。バイナリ結果はエラーです（skill body はテキストでなければなりません）。
5. **Provenance 分類（#3196）**: ステップ 2 由来の builtin/plugin、または `resolved_path` が `ctx.available_skills`（`:skill` invocation が解決するのと SAME の registered-skill スナップショット）内のエントリに一致する場合は `config_entry`。3 つのクラスのいずれにも一致しないパスもそのまま返されます — プレーンで、展開されず、イベント無し（fail closed）。
6. **展開**: provenance が設定されている場合、`reyn.plugins.skill_load.load_skill_body` は `${REYN_PLUGIN_ROOT}`/`${REYN_SKILL_DIR}`/`${REYN_PROJECT_DIR}`/`${CLAUDE_*}` エイリアスを無条件に展開し、`${env:VAR}` は `VAR` が `ctx.permission_decl.env_expand`（#3198、deny-by-default）で宣言されている場合にのみ展開します — 宣言されていない、または未設定の名前のトークンは展開されずに残ります（決して blank にはなりません）。**#3629**: これが `content` として返される完全展開済みの文字列です（モデルがこのターンで読むもので、変更なし）；`load_skill_body` は追加で、`${REYN_SKILL_DIR}`/`${REYN_PLUGIN_ROOT}`（+ `${CLAUDE_*}` エイリアス）を LITERAL のまま残した永続化安全な variant と、location-token map も返します。これはこの op 結果の `content_history`/`token_map`/`skill_source_path` フィールドとして表面化します — 理由はステップ 8 参照。
7. **自己 bound**: 過大な body はリゾルバーのインラインキャップ（`control_ir_inline_cap`）に truncate され、コンテキストを溢れさせません；そのパスでは `status: "truncated"` + `note`。
8. **#3629 — 展開済みの値ではなく永続化安全な history**: `content` は現在のターンの LLM 呼び出しが見るものです；`content_history`（provenance が設定された場合のみ存在）は `router_loop.py` の tool-result assembly が代わりに `history.jsonl` に永続化するものです — history は immutable なので、絶対パスをそこに焼き込むと、後の rename/move（#3588 は 1 つの実例）が永遠に stale になり得る値を凍結してしまい、モデルには stale な絶対パスと live なものを区別する方法がありません。wire-serialise パス（`RouterHistoryBuffer._serialise_turn` → `reyn.plugins.skill_load.refresh_location_tokens`）は、永続化されたエントリが replay されるたびに、リテラルトークンを CURRENT のファイルシステムに対して新たに再解決します — `token_map` は監査完全性のためのメタデータのみです（決して再展開の source ではありません）。

結果フィールド: `status`（`"ok"` / `"truncated"` / `"not_found"` / `"error"`）、`path`、`content`、加えて truncated 結果には `total_chars`/`_truncated`/`note`、非 UTF-8 コーデックが使われた場合は `encoding`、provenance クラスが一致した場合（ステップ 6）は（#3629）`content_history`/`token_map`/`skill_source_path`。

発行されるイベント: `tool_executed`（`op="load_skill"`）は常に；`skill_body_loaded`（`provenance`、`env_tokens_expanded`/`env_names_expanded`、`env_tokens_denied`/`env_names_denied` — 名前とカウントのみで展開/拒否された値は決して含まない）は provenance が分類された場合のみ。

## `pipeline_install`

Pipeline（ローカル DSL ファイルまたは git/GitHub source URL から）をプロジェクトの `pipelines.entries` 設定に登録します。2 つの tool surface verb が同じ `op_runtime/pipeline_install.py` ハンドラーに収束します: `pipeline_install_local`（ローカルパス）と `pipeline_install_source`（git/URL）。`skill_install` を可能な限り反映し、その汎用の path-safety + sandboxed git-clone ヘルパーをそのまま再利用します（`_safe_skill_name` / `_contained_under` / `_parse_source_spec` / `_source_host` / `_shallow_clone` / `_read_yaml` / `_write_yaml` / `_resolve_project_root` は skill 固有のロジックを持ちません）。

ローカルパスの例:
```json
{
  "kind": "pipeline_install",
  "path": "pipelines/hello.yaml"
}
```

Source/git の例:
```json
{
  "kind": "pipeline_install",
  "source": "https://github.com/user/pipeline-repo"
}
```

サブディレクトリ規約（Terraform を反映、`skill_install` と同じ）: `"https://github.com/user/repo//pipelines/my-pipeline"` はクローンされたリポジトリ内の `pipelines/my-pipeline` サブディレクトリを選択します。

フィールド:
- `path`（`source` が無い場合は必須）— pipeline の `*.yaml` DSL ファイルへの直接パス。`skill_install` と異なり、ディレクトリかファイルかの解決はありません — pipeline の登録は常にちょうど 1 ファイルです。source install の場合、`path`（設定時）はリポジトリ root/サブディレクトリ相対で DSL ファイルを選択します；省略時、リポジトリ root/サブディレクトリはちょうど 1 つの `*.yaml` ファイルを含んでいなければなりません。
- `source`（省略可）— git または GitHub URL。ハンドラーはリポジトリを `.reyn/pipelines/<name>/` へシャロークローンします。リポジトリ内のサブディレクトリは `//` セパレータで指定します。呼び出し元の permission 宣言に `http.get: [{host: <source_host>}]` が必要です。
- `scope`（省略可、デフォルト `".reyn/config/pipelines.yaml"`）— 後方互換のため保持；現在未使用（すべての install は `.reyn/config/pipelines.yaml` に書き込みます）。
- `name`（省略可、#2722）— 自由な NAMESPACE キーで、宣言された `pipeline:` 名とは結合されていません。ファイル内のすべての `pipeline:` ドキュメントは `{name}.{declared-name}` として登録されます；`.` は予約済み（namespace セパレータ）で `name` 内では拒否されます。省略時、キーは DSL ファイルの stem（または git install の場合は source のベース名）にデフォルトします。
- `plugin_id`（省略可、ADR 0064 §3.7、plugin model P2）— `skill_install` の `plugin_id` フィールドをそのまま反映します（同じ stamp-on-entry メカニズム、`plugin_install` の内部呼び出しでのみ設定）。

ハンドラーのライフサイクル（source パスはステップ 1 の前に 0a〜0d を挿入）:
0. **Source パスのみ**: (a) source host に対して `require_http_get` でゲート。(b) 候補名を sanitize し、クローン先が `.reyn/pipelines/` 配下に含まれることを検証 — どちらかが失敗した場合、ファイルシステムの変更前に拒否します（path-traversal → arbitrary-rmtree ガード）。リポジトリを `.reyn/pipelines/<candidate_name>/` へシャロークローン。(c) DSL ファイルを探す（`path` が選択するか、リポジトリ root/サブディレクトリの唯一の `*.yaml` ファイル）。(d) 宣言された名前が解決され sanitize された後、containment チェック + 名前が candidate と異なる場合はクローンディレクトリをリネーム。
1. DSL ファイルパスを解決（ローカル: `op.path` を直接；source: located されたクローンファイル）
2. `parse_pipeline_docs` でパース — 1 ファイルが複数の `pipeline:` ドキュメントを持つ場合があります（#2722）；不正な形式のファイルは拒否され（`status="error"`）、決して登録されません
3. 登録 namespace キーを解決（#2722）: `op.name` または DSL ファイルの stem（source install の場合: クローン前に導出された sanitize 済み candidate）；`.` は拒否されます
4. `content_guard.scan_for_threats(scope="strict")` で**すべての** pipeline ドキュメントの description を threat-scan — blocking-severity のマッチでブロック（source パス: ブロック時はクローンを削除）
5. `PermissionResolver.require_file_write`（= `.reyn/config/pipelines.yaml`）でゲート
6. `.reyn/config/pipelines.yaml` に `pipelines.entries.<name>` を `{path, description, enabled: true}` で書き込み（+ 設定されていれば `source: <url>` / `plugin_id: <id>`）
7. `record_config_generation` を呼ぶ（recovery-core: 切り詰め耐性スナップショット、#2259 / CLAUDE.md gate）
8. `pipeline_installed` イベントを発行（P6 監査証跡）— この install が登録する `{key}.{declared-name}` グローバル名の FULL セットである `registered_names` を持ちます（#2722 H6）
9. `get_active_hot_reloader().request_reload(source="pipeline_install")` 経由で hot-reload をリクエスト（既存の `"pipelines"` シーム — `Session._reapply_pipelines` — がレジストリを再構築）

結果フィールド: `status`（`"installed"` / `"blocked"` / `"error"`）、`name`、`registered_names`、`path`、`description`、`config_path`、`source`（ローカル install の場合は空文字列）。

発行されるイベント: `pipeline_install_threat_match`、`pipeline_install_threat_blocked`（threat scan）、`pipeline_installed`（成功時 P6）。

## `presentation_install`

名前付きの presentation テンプレート（宣言的なコンポーネントツリー）をプロジェクトの `presentations.entries` 設定に登録します（proposal 0060 Phase 1 Layer A、A8）。1 つの tool surface verb: `presentation_install_local`、`op_runtime/presentation_install.py` が処理します。`skill_install` / `pipeline_install` の STRUCTURE（permission ゲート → 設定書き込み → `record_config_generation` → イベント発行 → hot-reload）を反映しますが、source/git-fetch パスは**無く**（blueprint はインラインで運ばれる小さな宣言的データで、決して file-backed の artifact ではない）、`scan_for_threats` の呼び出しも**ありません** — present blueprint は構造的に非実行であることが保証されています（`reyn.core.present.catalog`: 8 個の固定コンポーネント、非リテラルの値はすべて `$bind` RFC-6901 JSON-Pointer、template-ref/eval/exec のサーフェス無し、`image.src` はラベルとしてレンダリング — fetch/SSRF 無し）；`validate_blueprint`（インラインの `present(blueprint=...)` op が既に通過するのと SAME のゲート）が、skill/pipeline の自由記述 `description` に対する `scan_for_threats` と同じ役割を果たします。

例:
```json
{
  "kind": "presentation_install",
  "name": "status_card",
  "blueprint": {
    "component": "keyvalue",
    "rows": [{"label": "status", "value": {"$bind": "/status"}}]
  }
}
```

フィールド:
- `name`（必須）— `presentations.entries` の設定キー；`present(view=<name>)` op が解決する値。
- `blueprint`（必須）— 宣言的なコンポーネントツリー。インラインの `present(blueprint=...)` の `blueprint` フィールドと同じ形状です。

ハンドラーのライフサイクル:
1. 構造的な threat ゲート: `validate_blueprint(op.blueprint)` — 不正な形式 / 非カタログの blueprint に対しては、設定変更の**前に**拒否します（`status="blocked"`）。
2. `PermissionResolver.require_file_write`（= `.reyn/config/presentations.yaml`）でゲート
3. `.reyn/config/presentations.yaml` に `presentations.entries.<name>` を `{blueprint, enabled: true, provenance: <ctx.turn_origin>}` で書き込み — `provenance` は `ctx.turn_origin` のみから OS が刻印します（A7/A9）、op フィールドからは決して来ません
4. `record_config_generation` を呼ぶ（既存の設定クラッシュリカバリを継承；新しい recovery-gated obligation は無し — この op に truncate-falsify テストの義務はありません）
5. `presentation_installed` イベントを発行（P6 監査証跡）
6. `dispatch_install_reload(source="presentation_install")` 経由で hot-reload をリクエスト（既存の `"presentations"` シーム — `Session._reapply_presentations`、FP-0054 PR-C — がレジストリを再構築；operator が `presentations.yaml` を編集した場合に既に reload されるのと SAME のシーム）

構造的に inert な状態で出荷されます: presentation は名前で invoke されるものです — `present(view=<name>)` op がそれを名指しした時のみレンダリングされるため、install された直後の template は discoverable だが、参照されるまでは dormant です（新しい状態は不要、skill/pipeline の builtin-inert を反映）。

結果フィールド: `status`（`"installed"` / `"blocked"` / `"error"`）、`name`、`config_path`。

発行されるイベント: `presentation_install_blocked`（構造的ゲート）、`presentation_installed`（成功時 P6）。

## `plugin_install` / `plugin_uninstall`

ADR 0064（plugin model）P2 のインストール機構です。plugin は自己完結したディレクトリです（plugin root の `plugin.json` マニフェスト + 任意の `mcp`/`pipelines`/`skills` サブディレクトリ、ADR §3.1、`.reyn-plugin/plugin.json` から移設 — #4570 conversion A、Agent Plugins 1.0 標準のマニフェスト位置への整合）— `plugin_install` はそれを `~/.reyn/plugins/<name>/`（グローバル、一度）へコピーし、ファイル種別ごとに安定位置トークンを焼き込み（下記 step 6 — `pipelines/*.yaml`/`SKILL.md` には `${REYN_*}`、`mcp.json` には標準自身の `${PLUGIN_ROOT}`/`${PLUGIN_DATA}`）、plugin ディレクトリが含む capability kind（`mcp`/`pipelines`/`skills`、`capability_kinds_present` によりディレクトリ/ファイルの存在から純粋に導出 — #4570 conversion B でマニフェスト自身の `capabilities`/`entries` フィールドが削除されたため、もはやマニフェスト宣言ではありません）を、既存の `skill_install` / `pipeline_install` が既に提供する SAME verb を呼ぶことで REGISTER します（加えて、任意の root `mcp.json` については `.reyn/config/mcp.yaml` への直接書き込み）— これは orchestration 層であり、4 つ目のレジストリではありません。

**Register-only**（#3209 — architect-firm redesign、owner GO 2026-07-23）: `plugin_install` は plugin の外部 Python 依存を決して provision しません。#3209 以前の設計は install 時に per-plugin venv（`<sys.executable> -m venv` + `pip install`）を実体化し、`command: "python"` の mcp エントリをその venv のインタープリタへ書き換えていました — registration op に乗った、本来無関係な env-provisioning の責務でした。そのステップ全体（2 つのインタープリタパス解決子、venv 実体化呼び出し、`_deps_materialised` install-state ステージ）は clean-break で REMOVED、移行 shim 無しです。plugin の `requirements.txt`（存在する場合）は今や plugin_install がコピーはするが決して読まないだけの inert データです: 外部依存は**skill-driven** です — install する skill の SETUP 指示が operator/LLM を自分自身の venv 作成、その中での `pip install -r requirements.txt`、plugin の `mcp.json` の server `command` をその venv の python インタープリタの絶対パス（Windows: `Scripts\python.exe`）へ直接向けることを案内します。`plugin_install` は plugin の `mcp.json` が名指す `command` をそのまま登録します — いかなる書き換えもありません。**Fail-fast は維持されます**（#3060 by-construction requirement）: 不完全/欠落した venv を名指す `command` は MCP spawn 時に明確な OS レベルのエラーで失敗します；plugin_install/spawn は決してランタイムフェッチにフォールバックしません。この redesign が置き換えるインタープリタパス解決の歴史は ADR 0064 §3.11a を参照してください。`op_runtime/plugin_install.py` / `op_runtime/plugin_uninstall.py` が処理します。LLM tool surface: `install_plugin` / `uninstall_plugin`（`tools/plugin_management_verbs.py`）— op kind との canonical-declaration 衝突を避けるため別名です（`mcp_install_local` vs. `mcp_install` の op-kind 前例を反映）。

ADR §3.9（P3）: 同じ typed op は slash command（`/plugin install builtin|local|git <SOURCE> [as <INSTALL_NAME>]` / `/plugin uninstall <NAME>`、`interfaces/slash/plugin.py`）と CLI command（`reyn plugin install builtin|local|git <SOURCE>` / `reyn plugin uninstall <NAME>`、`interfaces/cli/commands/plugin.py`）としても公開されています — どちらも `ToolContext` を組み立てて `invoke_tool(get_default_registry(), "install_plugin"/"__uninstall", ...)` を呼ぶ薄いアダプタで、live chat-router の LLM tool call が使うのと SAME の lookup+dispatch です。どのサーフェスもセキュリティロジックを再実装しません: 複合の permission decl は `tools/plugin_management_verbs.py`（tool wrapper）で一度だけ宣言され、`{kind: "git"}` の run-code trust ゲート自体（下記）は 1 層下の `core/op_runtime/plugin_install.py::handle` に存在します — すべてのサーフェスがこの op ハンドラーに流れ込みます。slash サーフェスはセッションの LIVE な `RouterHostAdapter.make_router_op_context` をスレッドします（本物の intervention bus — `{kind: "git"}` install はインタラクティブにプロンプトを出し、OpContext は `#1339` の sandbox floor（`resolve_sandbox_policy`、write_paths はデフォルトで workspace に制限）を持つため、`/plugin` から `{kind: "local"}`/`{kind: "git"}` の plugin を install するには、`~/.reyn/plugins/` をカバーする operator の `reyn.yaml` `sandbox.policy.write_paths` の許可が追加で必要です — live な LLM tool call と同様）。CLI サーフェスは代わりに standalone な `OpContext` を直接構築します（`build_router_op_context` 無し、sandbox floor 無し — `reyn mcp install` の CLI-is-the-operator-trusted-entry-point 前例を反映）。その `interactive` フラグは `not --non-interactive and sys.stdin.isatty()` です。どちらのサーフェスでも、非インタラクティブな呼び出し元（intervention bus 無し）は `{kind: "git"}` の run-code trust ゲートを閉じたまま失敗します — そのゲートは無条件の deny-else です（`require_plugin_git_run_code_trust`）、sandbox floor とは独立です。

CLI の floor-bypass が構造的に安全なのは、LLM の `~/.reyn/plugins/` への到達が 2 つの層で閉じられており、CLI は operator のみが到達可能（LLM は不可）だからです: (1) OpContext 層のゲート — LLM が到達可能などのパス（tool/slash）でも `#1339` の sandbox floor + `require_file_write` が、明示的な operator の許可無しに `~/.reyn/plugins/` への書き込みを拒否します；(2) OS 層 — `~/.reyn/plugins/` へ直接書き込もうとする LLM の `exec` でさえ、強制される exec policy の `write_paths` が workspace-tight（`resolve_sandbox_policy` の floor = `[workspace.base_dir]`、#1326 により LLM op フィールドより operator が勝つ）で、Landlock/Seatbelt は `write_paths` の外への書き込みを deny-by-default で拒否するため、sandbox backend により拒否されます — そして `~/.reyn/plugins/`（`$HOME` 配下）は決して workspace の許可の下にありません。したがって operator のみの CLI が OpContext 層の floor をスキップしても、LLM が到達できたはずのものは何も除去しません。

`plugin_install` の例（typed discriminated `source`、§3.8 — form-sniffed な文字列は決して使いません）:
```json
{
  "kind": "plugin_install",
  "source": {"kind": "local", "path": "/path/to/my-plugin"},
  "name": "my-plugin"
}
```

`source` は正確に次のいずれか 1 つです:
- `{kind: "builtin", name: "<name>"}` — reyn 自身が出荷する `src/reyn/builtin/plugins/<name>/` 配下の plugin。RCE trust risk 最低。
- `{kind: "local", path: "<dir>"}` — LLM が作成/テストしたローカルディレクトリ（ADR §3.2 の主要な日常的「promote」ループ）、または既にディスク上にある手書きの plugin。RCE trust risk 中程度。
- `{kind: "git", url: "<url>"}` — リモート git URL、シャロークローンされます。RCE trust risk 最高 — 個別の per-install run-code trust 判断（`require_plugin_git_run_code_trust`、下記のゲート 2）でゲートされ、fetch 軸とは分離されています；リモートコードの fetch と実行は明示的な operator-trust の判断であり、決して自動実行されず、決して事前許可できません。

フィールド（`plugin_install`）:
- `source`（必須）— 上記の discriminated union。
- `name`（省略可）— マニフェスト自身の `name` を install-directory / registry-provenance キーとして上書きします。

フィールド（`plugin_uninstall`）:
- `name`（必須）— plugin の install 名。

Permission ゲート（§3.10 — EXISTING ゲートから合成、新しい bool axis 無し；#571 collapse arc が旧 bool-axis パターンを削除）:
1. **グローバルコピーの書き込み** — `~/.reyn/plugins/<name>/` への `require_file_write`。このパスはデフォルトの書き込みゾーン（CWD 配下の `.reyn/`）の外にあるため、既存ゲートの「zone OR approved」decl-less ルールが、明示的な承認 / JIT ask 無しで既にこれを拒否します — 新しいゲートは不要です。
2. **`{kind: "git"}` run-code trust** — fetch の前にチェックされる専用の `require_plugin_git_run_code_trust` ゲート。これは RCE trust boundary で、意図的に `require_http_get`（fetch 軸）とは**分離**されています: バイトを fetch することとそれを実行することは別の判断です。`require_http_get` は per-host、PERSISTENT（ALWAYS → `approvals.yaml`）で、`web.fetch` と SHARED です。したがって web fetch のために一度許可された host が、それによって plugin コードの install + 実行まで許可してしまうと、その host は将来のすべての git plugin にとって恒常的な silent-RCE grant になってしまいます。run-code ゲートはどの approvals map も参照/書き込みしません（キー無し、ALWAYS パス無し、`reyn.yaml` の事前許可無し）；その選択肢集合（`plugin_run_code_trust_choices`）は yes/no のみを提供するため、毎回の install で再度尋ねられ、決して事前許可できません（§3.10「決して自動実行しない」）。Fail-closed: 非インタラクティブな呼び出し元は拒否されます。clone host に対する `require_http_get` はその後も実行されます（多層防御としてのネットワーク到達性）が、`{kind: "git"}` を安全にするのは run-code ゲートです。

名前衝突の優先順位（§3.8/§3.10）: `~/.reyn/plugins/<name>/` が既に**別の種類**の完了済み install を保持している場合、`reyn.plugins.source.resolve_name_collision` が勝者を決定します（`builtin <= local << git`）— 信頼度の低い source は拒否され（`status="skipped"`）、決して信頼度の高いものを黙って上書きしません。

`plugin_install` ハンドラーのライフサイクル（one-shot）:
0. Reconcile: `.reyn-plugin/_install_state.json` マーカーを残した、以前のクラッシュ/中断した install が残した `~/.reyn/plugins/<name>/` は、この install が進む前にロールバックされます（`reconcile_plugin_installs`、§3.11 — 次回の `plugin_install` 呼び出し時の self-healing。このリポジトリには汎用の process-startup フックが無いため、「次回使用時」がドキュメント化された reconcile トリガーです）。ロールバックは uninstall の**drop-registry-first** の順序を反映します: 一部の capability を登録した後にクラッシュした partial install は、これから削除されるディレクトリを指す `plugin_id` タグ付きのレジストリエントリを残しているため、それらのエントリはコピーが削除される**前**に 3 つの `.reyn/config/*.yaml` レジストリすべてから削除されます（ungated — 既に壊れたエントリの OS-internal な修復）。そうしないと dangling なレジストリエントリが残ってしまいます。実際にロールバックされた plugin ごとに `plugin_install_reconciled`（`name`、`action` — 現状 `"rolled_back"` のみ）を発行します。
1. `source` をその `kind` に応じてソースディレクトリへ解決し、source のゲートを適用します: `{kind: "git"}` はクローン前に run-code trust ゲート（2）に続いて `require_http_get` を実行；`builtin`/`local` はネットワークに触れません。
2. `reyn.plugins.manifest.load_plugin_manifest`（P1）経由で `plugin.json`（plugin root）をロード + 検証 — 欠落/不正な形式のマニフェストはコピーの**前**に拒否されます（`status="error"`）。
3. 名前衝突の優先順位チェック（上記）。
4. ゲート 1（グローバルコピーの書き込み）。
5. コピー: `_install_state.json` マーカーを書き込み、その後 source ツリー（VCS メタデータは除外）を `~/.reyn/plugins/<name>/` へコピーします。`plugin_install_copied` を発行。
6. コピーのテキストファイルへ安定位置トークンを焼き込みます（`_expand_plugin_files`）— ファイル種別ごとに 3 通りの異なる bake です:
   - `mcp.json`: `${PLUGIN_ROOT}`/`${PLUGIN_DATA}` — Agent Plugins 1.0 の `mcp.schema.json` 自身のトークン語彙であり、`${REYN_PLUGIN_ROOT}` ではありません（#4570 conversion D、`_bake_mcp_json_fields`）— FIELD-AWARE です: `args`/`env` の値/`cwd` のみ展開され、`command`/`url` は決して展開されません（標準自身が引く injection boundary — plugin 作者の文字列が、このモジュールが解決するトークン経由で「何を実行するか」「どこへリクエストするか」を選べてはならない、という境界です）。テキストではなく JSON としてパースするため、`command`/`url` の中に偶然トークンが現れても、文字列一致で巻き込まれずリテラルのまま残ります。`${PLUGIN_DATA}` は `plugin_data_root()/<name>` — `~/.reyn/plugin-data/<name>/` に解決されます。`~/.reyn/plugins/` の SIBLING であり、コピー自体のサブディレクトリでは決してありません（step 5 のアトミックな swap がこのコピーを再インストール/更新のたびに丸ごと置き換えるため、再インストールを生き延びる必要のあるものはコピー内部に置けません）。bake のたびに eager に作成されます。
   - `pipelines/*.yaml`: 変更なし — #4570 以前からの reyn-native な `${REYN_*}` フルテキスト bake（P1 の `reyn.plugins.tokens.expand_reyn_tokens`、コピー時のコンテキストが値を持つすべてのトークン）。pipeline は標準が定義しない reyn の拡張であるため、conversion D はこの候補を意図的に触っていません。
   - `skills/*/SKILL.md`: より狭い焼き込みを受けます: `${REYN_PLUGIN_ROOT}` のみ（`plugin_install.py` の `_bake_plugin_root_only`）— `${REYN_SKILL_DIR}` と `${REYN_PROJECT_DIR}` は意図的にリテラルトークンのまま残され、skill-load verb（`reyn.plugins.skill_load.load_skill_body`、P4/#3070）が invocation のたびに新たに解決します。plugin のグローバルな `~/.reyn/plugins/<name>/` コピーは複数のプロジェクトへ enable される可能性がある（§3.3）ためです — 1 回の install 呼び出しのプロジェクトを共有コピーに焼き込んでしまうと、後で enable するすべてのプロジェクトが最初に install したプロジェクトに凍結されてしまいます。

   **stale-token 警告（#4610）**: 上記 2 つの語彙はファイルごとであり互換ではありません — plugin 作者があるファイルに対して間違ったトークンを推測した場合（`pipelines/*.yaml`/`SKILL.md` の中の `${PLUGIN_ROOT}`、あるいは `mcp.json` の `args`/`env`/`cwd` の中の `${REYN_PLUGIN_ROOT}`/`${REYN_PROJECT_DIR}`/`${REYN_SKILL_DIR}`）、以前はどちらの bake にも展開されずリテラル文字列のまま黙って生き残っていました。3 つの bake 関数はいずれも、自身の展開後出力を「もう一方の語彙」のトークン形（`_stale_token_warnings`）についてもスキャンし、見つかれば該当ファイルとその場所の正しいトークン名を含む警告を追加するようになりました — `mcp.json` の `command`/`url`（意図的に非展開であり、推測ミスではない）や、そもそも bake が触れないトークンには決して発火しません。report-only です: install をブロックすることは決してなく、結果 dict で開示されます（下記）。「報告すべきものが無ければ空」という `skipped`（#4580）と同じ形です。各 finding は `plugin_install_token_vocabulary_mismatch`（payload: `name`、`warning` — 結果 dict と同一の文字列、再導出なし）も発行します — 永続的な監査証跡側の対応物であり、一度 install されて二度と再検査されない plugin でも記録が残ります（#4610 PR-2）。
7. 登録（#3209: register-only、依存の実体化ステップ無し。#4570 conversion B: capability の有無はディレクトリ/ファイルの存在から導出され、マニフェストはもう `capabilities` フィールドを持ちません）: `mcp.json`（旧 `.mcp.json`）は常に probe されます（存在しなければ穏やかに no-op）— `.reyn/config/mcp.yaml` への直接書き込み（probe-then-commit、`mcp_install_local` を反映）。`pipelines/` ディレクトリが存在すればその `*.yaml` 全てを `pipeline_install.handle` で、`skills/` ディレクトリが存在すればそのサブディレクトリ全てを `skill_install.handle`（各サブ op は `plugin_id=<name>` を持つ、§3.7）で登録します — server の `command` はそのまま登録され、venv-interpreter の書き換えはありません。`plugin_install_registered` を発行。
8. `_install_state.json` マーカーを削除（不在 = 完了）し、`plugin_install_completed` を発行。

`plugin_uninstall` ハンドラーのライフサイクル（drop-registry-first、§3.11 — 中断された uninstall が、削除されたコピーを指す live なレジストリエントリを残すことは決してありません）:
1. `plugin_id == name` とタグ付けされたすべての `.reyn/config/{mcp,pipelines,skills}.yaml` エントリを削除します（実際に触れた設定ファイルごとに `require_file_write` でゲート）。`plugin_uninstall_registry_dropped` を発行。
2. `~/.reyn/plugins/<name>/` のコピーを削除します（`require_file_write` でゲート）。`plugin_uninstall_completed` を発行。

**Plugin data は uninstall を生き延びます**（#4570 conversion D）: plugin の `~/.reyn/plugin-data/<name>/` ディレクトリ（上記 step 6）は `plugin_uninstall` によって決して削除されません — データがコードより長く生きる方が安全な方向です；逆（コードが後で再インストールされ、古いデータが黙って消えている）は回復不能です。無言でもありません: uninstall した名前に対応する data ディレクトリが存在する場合、結果 dict は正確な場所を示す `plugin_data_retained_at` を持ちます。

**WAL-derived ではありません**（§3.11）: `~/.reyn/plugins/` のコピーは FILES であり、WAL-event-derived な状態ではありません — CLAUDE.md の truncate-falsify recovery gate はこれらには適用されません。上記の reconcile はファイルシステム/レジストリの整合性チェックです；レジストリエントリ自体は `skill_install` / `pipeline_install` 経由の既存の config-generation recovery パスに引き続き乗ります。

結果フィールド（`plugin_install`）: `status`（`"installed"` / `"skipped"` / `"error"`）、`name`、`plugin_root`、`source_kind`、`capabilities`、`registered`（capability ごとのサブ結果）、`stale_token_warnings`（文字列のリスト、bake が何も検出しなければ空 — #4610、上記）。

結果フィールド（`plugin_uninstall`）: `status`（`"uninstalled"` / `"error"`）、`name`、`removed`（削除されたエントリ名のレジストリごとのリスト）、`copy_removed`、`plugin_data_retained_at`（uninstall した名前に対応する `~/.reyn/plugin-data/<name>/` ディレクトリが存在する場合のみ、#4570 conversion D）。

発行されるイベント: `plugin_install_reconciled`（step 0、上記 — 以前の壊れた install の self-healing ロールバック、`_started` より前に、見つかった場合のみ発行）/ `plugin_install_started` / `_copied` / `_registered` / `_completed` / `_token_vocabulary_mismatch`（#4610、stale-token finding ごとに 1 件、上記）；`plugin_uninstall_started` / `_registry_dropped` / `_completed`。

## `embed`

生の embedding primitive（FP-0057 Phase 1）: テキストのバッチを入力し、順序を保ったまま 1 テキストにつき 1 ベクトルを出力します。`embed` は**ユーザー向け** primitive です — ユーザーは `embed` を自分の外部 MCP vector-DB の store/search ツールへ pipeline で組み合わせます（reyn 自身はユーザー向け RAG store をホストしません）。同時に、後続の内部 RAG op（`index_update` / `semantic_search`、FP-0057 Phase 2）が呼ぶ SHARED ロジックでもあります — 同じ `EmbeddingProvider`、embed ロジックの重複無し、audience サーフェスによる分割のみです。

```json
{
  "kind": "embed",
  "texts": ["first chunk of text", "second chunk of text"],
  "embedding_model": "standard"
}
```

フィールド:

- `texts`（list[str]、必須）— embed するテキスト。返されるベクトルはこの順序を保持します。
- `embedding_model`（str、デフォルト `"standard"`）— モデルクラス（light/standard/strong）または provider のモデル id リテラル。`EmbeddingProvider.embed` に転送されます。

返り値: `{"kind": "embed", "vectors": list[list[float]], "model": str, "total_tokens": int, "cost_usd": float | None, "priced": bool}`。cancel 時: `{"kind": "embed", "status": "cancelled", "model": str}`（下記の**Bound + cancel** 参照）。

`cost_usd` / `priced`（FP-0063 PC）: 呼び出しのコストは `estimate_embedding_cost` で価格付けされます（chat completion 向けに既に `pricing.py` が使っている同じ `litellm.model_cost` ルックアップを embedding モードのエントリに拡張したもの — 新しいレートテーブルではありません）。litellm が `model` を価格付けできない場合は `priced=False` + `cost_usd=None` — 未価格 / 未知のモデルは VISIBLE に degrade します（既存の `estimate_cost` 未知モデル sentinel、#1829 と同様、決して黙って `$0.00` にはなりません）。この支出は独立した embedding-cost aggregate（`llm/pricing.py` の `EmbeddingCost`）に `ctx.budget_gateway`（配線済みの場合）経由で記録されます — 単一の記録エントリポイント（`BudgetGateway.record_embedding`）が全 scope へ自ら fan-out します: session（gateway 自身の aggregate）と agent/project（gateway が保持する process-shared `BudgetTracker`）。fan-out が gateway に存在するのは、tracker とセッションの agent NAME（per-agent カウンタが使うキー）の両方を保持する唯一のオブジェクトだからです — op handler が持つのは `ctx.agent_id`（FP-0016 の host identity、別の値）のみなので、そこから記録すると誰も参照しないキーの下に支出が記録されてしまいます。意図的に chat の `CostBreakdown` には折り込まれません（embedding は input-only / 構造的に uncacheable であり、そうすると chat-call 専用の figure である `cache_hit_rate` / `cache_savings` が希釈されてしまいます）。per-scope の reader は `Registry.agent_embedding_cost` / `.project_embedding_cost` と `BudgetGateway.embedding_cost` を参照してください。

既存の `EmbeddingProvider`（`get_provider` 経由の `LiteLLMEmbeddingProvider` — 唯一の embedder。#3128 でプロセス内 sentence-transformers backend とその `RoutingEmbeddingProvider` prefix-dispatch wrapper が削除されたため、`get_provider` は今や litellm-backed provider を直接返します）を再利用します；この op は薄い typed envelope であり、再実装ではありません。バッチング（`embedding.batch_size`、デフォルト 100）は provider 内部で行われます — op 契約自体は list-in/list-out、batch 粒度です。

**Redaction-egress シーム**: API-backed provider 経由の embedding は外部 embedding API へテキスト content を送信します — データ egress ポイントです。バッチ内のすべてのテキストは、`provider.embed()` が呼ばれる**前に**、無条件に（呼び出し側による bypass 無し）PRE-embed スキャン（`redact_secrets`、既存の FP-0050 secret-redaction primitive）を通過します。redaction hit は `embed_secret_redacted` 監査イベントを発火します。これは既存の汎用 secret-redaction パスを使った Phase 1 の scaffold であり、完全な firm な ephemeral-attachment content policy は FP-0057 Phase 3 です。

**Bound + cancel**（#3043）: OS の他のあらゆる provider 呼び出しと同様に、embed は bound かつ cancellable です。**bound** は `embedding.timeout`（デフォルト 60.0 秒、`<= 0` で無効化）で、provider 内部で**試行ごと**に適用されます — つまりこの op だけでなく provider のすべての呼び出し元をカバーします。これが無いと唯一の上限は litellm 自身の `request_timeout` デフォルト（6000 秒/試行、`max_retries` 全体で約 5 時間）で、operator にはハングと区別がつきません。

この bound は**レイテンシ**の不変条件であり、**コスト**の不変条件ではありません: reyn が待つ時間を制限するのであって、provider が受け取るリクエスト数を制限するものではありません。修正前は OpenAI SDK クライアント自身のリトライ（`max_retries=2`、litellm の暗黙のデフォルト）が bound の**内側**にあったため、1 回の試行で最大 3 リクエストが送られ、`max_retries: 3` は最大 9 リクエストになり得ました — デフォルトの 60.0 秒 bound 下で 7.6 秒で配信されたと計測されており、bound は一切作動しません。[#3054](https://github.com/tya5/reyn/issues/3054) がこのレバーを閉じました: `_aembedding_bounded` が `litellm.DEFAULT_MAX_RETRIES = 0` を設定する（`max_retries=0` という falsy な kwarg 単独では `x or DEFAULT` の罠が復活する）ため、SDK 内部のリトライは無効化され、reyn 自身の `_embed_batch_with_retry` ループが唯一のリトライ層になります — `max_retries: 3` は今や 9 でなく 3 の配信リクエストを意味します。`timeout` を下げてもこのカウントは変わりません。両者は別のレバーです。*残余*の under-count — コストトラッカーは返された ONE レスポンスのトークンのみを記録するため、成功前に N 回リトライした呼び出しは N 件中 1 件の配信リクエストしか報告しません — は `embed_attempts` 監査イベント（#3047 (c)、observation-only: コスト集計には一切触れないため二重カウントし得ない）によって OBSERVABLE に（価格付けはされず）なります。**cancel** シームはこの op にあります: `provider.embed()` は `race_cancellable`（`mcp` と `sandboxed_exec` が使うのと同じ primitive）経由で `ctx.cancel_event` と競走されるため、Ctrl-C は bound を待ちきる代わりに進行中の HTTP read を即座に中断します。この op が cancel 側の正しい altitude であるのは、すべての embedding egress がこの op を経由するからです（`semantic_search`、`index_update`、action-index はすべて `provider.embed()` を provider-direct で呼ばずに `embed` op を dispatch します — redaction-egress シームが依存しているのと同じ性質）。

イベント: PRE-embed スキャンが 1 件以上のテキストを redact した場合の `embed_secret_redacted`（`count`、`model`）。`cancel_event` が embed 中に発火した場合の `embed_cancelled`（`model`）— provider fault とは別の cancelled outcome（`mcp_cancelled` / `sandboxed_exec_cancelled` をミラー）。成功した embed ごとの `embed_attempts`（`model`、`attempts`、`successful_batches`、#3047 (c)）: `attempts` は reyn 自身のリトライループが provider 呼び出しに到達した回数（内部バッチを通算）、`successful_batches` は返された回数 — つまり `attempts - successful_batches` がコストトラッカーには見えないリトライオーバーヘッドです（返されたレスポンスのみ価格付けするため）。リトライがゼロでも成功時は常に発行される（`attempts == successful_batches`）ため、イベントが無いことは「instrumented されていない」を意味し、「リトライがゼロ」を意味しません。provider が供給する `attempts` は `EmbedBatchResult` 上で `NotRequired` です — loopless な provider はこれを省略し、op は単に発行しません（`attempts=1` を捏造しない）；op はこれを defensive に読みます。これは reyn のリトライループの altitude であって、生の wire-request カウントではありません — 両者が一致するのは #3054 の `max_retries=0` が SDK 内部リトライを 0 に保っている間だけです。

Default-**ALLOW**（compute op — コストは embedding API/compute であって workspace への書き込みではない）；登録済みの router-callable ツールとして、RouterLoop ゲート（`effective.tool_contextually_denied`）での per-session の contextual narrowing により個別に name-gate 可能です。Phase 1 では、この op は追加的であり `embed_and_index`（`reyn.api.safe.embed_index`、CodeAct 専用の ingestion entry）を retire しませんでした；そのクリーンブレイクは FP-0057 Phase 2b で着地しました — `embed_and_index` は削除され、`index_update`（ingestion）と `semantic_search`（query）はどちらも今や embed 呼び出しをこの op 経由でディスパッチします（下記の [`index_update`](#index_update) 参照）。

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

## `index_update`

ソースの `IndexBackend` への差分 / delta-reconcile ingestion です（FP-0057 Phase 2a）。**フルリビルドモードはありません** — ゼロからのリビルドは `index_drop` → 空になったソースへの `index_update` です。呼び出し元（chunker）は事前に chunk 化した `chunks` を供給します；各 chunk はその `metadata` に `content_hash` + `source_path` を持ちます。ソースの現在の index と、各 `source_path` 内で `content_hash` によって content-addressed で reconcile されます:

- **add** — 新しい `content_hash`、新しい `source_path` → embed（`embed` op 経由 — 同じ primitive、embed ロジックの重複無し）+ insert。
- **update** — 新しい `content_hash`、`source_path` は既に index 済み（content が変わった）→ embed + insert；同じパスの古い hash は同じパスで削除されます。
- **remove** — index 済みの hash で、その `source_path` が今回の呼び出しの chunks に含まれるが hash が含まれない → 削除。今回の呼び出しが chunks を供給する `source_path` にスコープされます — 一切言及されないパスは触れられません（少数ファイルの部分的な再 ingest がソースの残りを大量削除することはありません）。
- **skip** — `content_hash` が既に index 済み → no-op（再 embed 無し）。

```json
{
  "kind": "index_update",
  "source": "project_docs",
  "chunks": [
    {"text": "...", "metadata": {"content_hash": "abc123", "source_path": "docs/a.md"}}
  ],
  "embedding_model": "standard"
}
```

フィールド:

- `source`（str、必須）— ingest 先の論理ソース名。
- `chunks`（list[dict]、デフォルト `[]`）— reconcile する chunk；それぞれ `{text, metadata}` で `metadata.content_hash` / `metadata.source_path` が必須。
- `embedding_model`（str、デフォルト `"standard"`）— このソースにまだ記録済みのモデルが無い場合（新しいソースへの最初の `index_update`）にのみ使用されます — 既に index 済みのソースの記録済みモデルが常に優先します（ソースは 1 つの embedding space です）。
- `description` / `path`（str、省略可）— `SourceManifest` のフィールド。最初の index 時に設定するか、上書きします。

**ソース・モデル束縛**: ソースの embedding モデルは最初の ingestion 時に記録され、そのソースへの以降のすべての `index_update` 呼び出しで再利用されます。

**コストの可視化**: `EmbeddingProvider.estimate_tokens` が embed 対象バッチ（PRE-embed dedup skip 後）に対して照会され、`embedding.cost_warn_threshold`（`reyn.yaml`）と比較されます。超過しても op はブロックされません — `index_update_cost_warning` 監査イベントを発行し、返される envelope が `cost_warning` フィールドを持つため、大きな ingestion は黙って embed されるのではなくコストを可視化します。

戻り値: `{"kind": "index_update", "source": str, "added": int, "updated": int, "removed": int, "skipped": int, "chunk_count": int, "embedding_model": str, "cost_warning": dict | null}`。

イベント: embed 対象バッチが設定済み閾値を超えた場合の `index_update_cost_warning`（`source`、`chunk_count`、`estimated_tokens`、`threshold`）；完了時の `index_updated`（`source`、`added`、`updated`、`removed`、`skipped`）。

Default-**ALLOW**（own-write op — 書き込むのはソース自身の index + manifest のみで、`index_drop` のような破壊的な cross-cutting op ではありません）。

## `compact`

会話履歴を*今*任意で圧縮し、コンテキストウィンドウを解放します。ウィンドウが埋まりつつあるとき、OS は**コンテキストサイズシグナル**（正確なトークン数の空きウィンドウを示す `## Context window` ヘッダー）を注入します；モデルは必須の `retry_loop` backstop を待つ代わりに `compact` を発行して応答できます。この op は呼び出し元が配線した圧縮（`force_compact_now`）へルーティングされ、その後の解放トークン数と空きウィンドウを正確なトークン数で報告します（media load-contract エラーと単位を揃えているため、「圧縮すべきか」と「今何が収まるか」が同じスケールを使います）。

```json
{
  "kind": "compact"
}
```

フィールド:
- `reason`（str、省略可）: 監査証跡向けの、モデルが供給する短い根拠。OS はこれを一切解釈しません。

戻り値:
- `status: "ok" | "error"`
- `freed_tokens: int` — 正確なトークン数の削減量。**構造上ほぼ 0**: router prompt は head+tail の*ターン*数で bound されている（`_build_history_for_router`）ため、圧縮しても bound されたビューは縮みません — 既に elide された中間部分を summary bridge に圧縮するだけです。ここでの `freed_tokens` を前面に出さないでください — 下記の圧縮メトリクスを参照。
- `free_window_after` / `free_window_before: int` — 圧縮後/前の正確なトークン数の空き容量。
- **圧縮メトリクス**（意味のあるシグナル）: `summarized_turns: int`（bridge に折り畳まれた古いターン数）、`compressed_tokens: int`（それらの生のトークンコスト）、`bridge_tokens: int`（summary のトークンコスト）。意味を持つのは `compressed_tokens → bridge_tokens` の圧縮であり、`freed_tokens` ではありません。
- エラー時: `error_kind`（ここに圧縮コンテキストが配線されていない場合の `compaction_unavailable`；`compaction_failed`）+ `error`。

**イベント**: `compact_op_requested` / `compact_op_completed`（`freed_tokens`、`free_window_after`、`summarized_turns` / `compressed_tokens` / `bridge_tokens`）/ `compact_op_failed` / `compact_op_unavailable`（P6）。内部の圧縮エンジンは自身の圧縮監査イベントを発行します。

**Permission**: 不要（LLM コストのみ）。任意であり、常に実行される involuntary な `retry_loop` backstop とは独立です。

**可視性**: ウィンドウが埋まりつつあるときのみ LLM に提示されます（tool / `available_control_ops`）— コンテキストサイズシグナルと対で — 圧縮するものが無いときには提示されません（`search_actions` の可視性ゲートを反映）。permission ゲートは常に「allow」のままで、*提示されるかどうか*のみがゲートされます。

## `emit_hook_event`

LLM が作成する hook-event の発行です（Hook-Event Redesign Phase 5 part 2、proposal [0059-hook-event-redesign.md](../../deep-dives/proposals/0059-hook-event-redesign.md) §8/§8.4）— LLM が live なセッション単位の `HookBus`（Phase 4a）に `HookEvent` を置ける**最初**の場所です；それ以前のすべての producer（10 の builtin ポイントでの `HookDispatcher.dispatch`、`Composer` の correlated output、Ingress Adapter）は OS-internal なコードであり、LLM の tool call では決してありません。Router 専用（`gates.router="allow"`）— ハンドラーには live な、session-bound な `HookBus` + `session_id` が必要で、それを配線するのは chat-router の `OpContext` のみです。

```json
{
  "kind": "emit_hook_event",
  "event_name": "deploy_ready",
  "payload": {"artifact": "build-42"}
}
```

フィールド:

- `event_name`（str、デフォルト `""`）— イベントの名前；router tool のスキーマはこれと `payload` のみを公開します。発行される kind は常に `llm:<session_id>:<event_name>` です — session コンポーネントは handler 実行時の `OpContext.session_id` のみに由来し、LLM が供給する値には決して由来しません（行儀の良い tool-call パスがこれを設定するための session フィールドはこのスキーマ上に存在しません）。スキーマ制約されています（#2890 F6）: `pattern=^[A-Za-z0-9_.-]*$` + `max_length=200` — 制御文字、改行、無制限の長さは Pydantic 検証時に拒否されます（ハンドラー自身の非空チェックが実行される前に）。したがってこれらは構築される `kind` や `hook_event_emitted` 監査イベントに決して流れ込みません。多層防御: いずれにせよ、kind は既に構造的にこのセッション自身の `llm:{session_id}:` prefix に閉じ込められています。
- `target_kind`（str | None、デフォルト `None`）— Pydantic モデル上の多層防御のエスケープハッチで、**router tool の JSON スキーマには意図的に露出されません**（通常の LLM tool call からは到達不能）。下記の kind ホワイトリストが、この Op の他の呼び出し元（例えば将来の Control-IR JSON サーフェス）に対する real で exercisable な subject を持つため、また security co-vet suite が reject パスを直接テストできるようにするために存在します。
- `payload`（dict、デフォルト `{}`）— 発行される `HookEvent` に、matcher / Composer が検査するために運ばれます；この op 自体によって hook message template にレンダリングされることは決してありません（§8.4 item 1 の `context_safe` テンプレート補間規律は Composer/render の関心事であり、emit の関心事ではありません）。

返り値: 成功時 `{"kind": "emit_hook_event", "status": "ok", "emitted_kind": str}`；autonomy boundary が emit を拒否した場合 `{"status": "denied", "error": str}`；不正な形式の `event_name`/`target_kind` の場合 `{"status": "error", "error": str}`。

イベント: `hook_event_emitted`（`kind`、`session_id`、`event_id`）— メタデータのみ。`hook_push_fired` の「メッセージ本文を決して含まない」規律を反映します（payload は LLM が作成した自由記述テキストを運ぶ場合があるため）。

**autonomy boundary（§8.4 item 3、セキュリティの要）は `HookBus.publish` の**前に**、2 つの別々の次元で強制されます**（`HookBus.publish` は同期的で、決して raise せず、すべての live な subscriber へブロードキャストします — イベントが bus に到達した後に下流のゲートはありません；ハンドラー（`reyn.core.op_runtime.emit_hook_event`）が唯一の防衛線です）:

1. **KIND 次元** — 静的な OUT-set ホワイトリスト（`reyn.hooks.schema_registry.is_emittable_llm_kind`、ALLOW-list であり DENY-list ではありません）: このセッション自身の `llm:<session_id>:*` namespace だけが発行され得ます。`builtin:*`（Reyn 自身の lifecycle/ingress イベントを spoof）、`composed:*`（Composer の CORRELATED output を spoof — LLM が Composer の実際の correlation ロジックを一切実行せずに `composed:*` のみの hook、例えば承認ゲート付きの deploy を発火させてしまう）、`webhook:*`/`mcp:*`（外部 ingress を spoof）、そして別セッションの `llm:*` はすべて拒否されます。
2. **SESSION 次元** — 通常の（`event_name`）パスに対して構造的です: kind の session コンポーネントは `ctx.session_id` のみから構築されます — 行儀の良い tool call がこれを上書きするためのスキーマ上のフィールドはありません。`target_kind` エスケープハッチは代わりに SAME のホワイトリストで検証されます — いずれの場合も、ハンドラーは session id で bus を lookup することは決してありません（`ctx.hook_bus` はこのセッション自身の bus への単一の固定参照です）。したがってホワイトリストチェックが無くても、不一致な kind のイベントを別セッションの bus へルーティングできるコードパスは存在しません。

**`OpContext.hook_bus`**（このセッションの `HookBus`、Phase 4a）は、この op が通す新しいシームです — `OpContext.hook_dispatcher` と同じ Session → router / kernel チェーンを通じてスレッドされます（そのフィールドのスレッディングを正確に反映: `Session.__init__` はセッションごとに 1 つの `HookBus` を構築し、`hook_dispatcher` と全く同じ方法でそれを `RouterHostAdapter` / `build_router_op_context` に渡します）。下流では、Composer が `composed:*` へ correlate した発行済みイベントは、既存の `composed:*` → `ComposedEventConsumer` → `HookDispatcher.dispatch_bus_event` → inbox `kind="hook"` の E-path（Phase 5 part 1、#2881）を変更無く通過します — `max_hook_driven_turns` ループバルブは、emit 起源の wake ターンを、新しい bound ロジックを一切追加せずにカウントします。これは Phase 5 part 1 が既に固定している「すべての wake パスは `kind="hook"` を通過する」不変条件と同じです。

**このフェーズのスコープ外**（#2884 で追跡、別の recovery-gated arc）: `_hook_driven_turns`（loop-valve カウンタ）をクラッシュを跨いで WAL/snapshot-backed にすること。これは in-memory-only のままです（proposal §11 future list item 2）；`emit_hook_event` は hook-driven-turn を生成するパスの数を増やしますが、このカウンタの crash-durability の姿勢は変わりません。#2884 はさらに、このフェーズの producer が表面化させる新しいリスク次元も追跡します: WAL-replay 駆動の再発行（クラッシュリカバリの WAL replay 中に再実行される `emit_hook_event` op）は、カウンタ自身の in-memory リセットとは別のハザードであり、これもここではスコープ外です。

---

**コントリビューター向けメモ:** `src/reyn/schemas/models.py` および `src/reyn/core/op_runtime/registry.py` に新しい Control IR op kind を追加する際は、**同じ PR でここにセクションを追加してください**。reference と registry は同期を保つ必要があります。ルールの詳細は [CLAUDE.md](https://github.com/tya5/reyn/blob/main/CLAUDE.md) を参照してください。

## LLM に op が提示される場所

OS は利用可能な op をすべてのコンテキストフレームに `available_control_ops` として注入します。各エントリーは `kind`、一行の説明、動作例を含みます。LLM は意図を説明にマッピングして op を選択します。Phase の Markdown は op の構文を説明してはなりません（P8）。

## 関連情報

- [events.md](events.md) — op の種類ごとに発行されるイベント
- コンセプト: principles P8 (principles doc removed)
