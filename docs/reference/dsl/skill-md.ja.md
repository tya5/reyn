---
type: reference
topic: dsl
audience: [human, agent]
applies_to: [skill.md]
---

# `skill.md` frontmatter

すべての Skill は `skill.md` を含むディレクトリです。その YAML frontmatter が Skill の構造を宣言します。

## スキーマ

```yaml
---
type: skill                    # 常に "skill"
name: my_skill                 # 一意の識別子
description: One-line summary  # `reyn skills` に表示
entry: <phase_name>            # 必須; 最初に実行される Phase
final_output: <artifact_type>  # 必須; Skill の結果のスキーマ
final_output_description: |    # 省略可能; 人が読める結果の説明
  ...
finish_criteria:               # 省略可能; クリーンな終了のための条件
  - All inputs validated
  - Final output passes the quality bar
graph:                         # 必須; 許可されるトランジション
  outline: [expand]
  expand: [end]
permissions:                   # 省略可能; 必要なケイパビリティを宣言
  shell: deny
  python:
    - module: stats
      function: compute
      mode: safe
required_credentials:          # 省略可能; このスキルが読み取れるクレデンシャルキー
  - github_token
imported_from: ...              # 省略可能; `skill_importer` が設定する出自情報
imported_at: 2026-04-29T...
imported_format: claude-skill
imported_revision: <git-sha>
---
```

## 必須フィールド

- **`type`** — `skill` でなければなりません。
- **`name`** — 解決とイベント相関に使用されます。
- **`entry`** — 開始する Phase の名前。`phases/` に存在しなければなりません。
- **`final_output`** — Skill が完了したときに生成される artifact 型。`artifacts/<name>.yaml` または stdlib の artifact として定義されている必要があります。
- **`graph`** — 隣接リスト。各キーは Phase 名、各値は許可される次 Phase 名のリスト。末端トランジションのマークには `end` を使用します。

## 省略可能なフィールド

- **`description`** — `reyn skills` に表示されます。
- **`final_output_description`** — Skill 詳細に表示される長い説明。
- **`finish_criteria`** — 終了が許可されるタイミングを Phase が知るために使用されます。
- **`permissions`** — 下記 [`permissions:` (skill-level)](#permissions-skill-level) を参照してください。
- **`required_credentials`** — 下記 [`required_credentials:`](#required_credentials-省略可能) を参照してください。
- **`postprocessor`** — 下記 [`postprocessor:`](#postprocessor) を参照してください。
- **`imported_*`** — `skill_importer` が書き込む出自フィールド。非アクティブ; パーサーはこれらを無視します。
- **`search_hints`** — 省略可; このスキルが答えられる例示クエリのリスト。カタログがルーターのコンテキストウィンドウを超える際の BM25/embedding 事前フィルタに使用される。大規模マルチスキルリポジトリでの recall 向上目的。
  例: `search_hints: ["記事を要約して", "tl;dr"]`

## `permissions:` (skill-level)

`permissions:` は `skill.md` frontmatter の **唯一** のパーミッション宣言場所です。Phase レベルのパーミッションは skill-only permissions migration で廃止されました。完全なセマンティクスとケイパビリティ階層については [permission-model.md](../../concepts/permission-model.md) を参照してください。

```yaml
permissions:
  shell: true                 # false（デフォルト）| true; シェル操作を有効化
  file.read:                  # CWD 外でスキルが読み取れるパス
    - path: ~/notes
      scope: recursive
  file.write:                 # デフォルト書き込みゾーン外のパス（.reyn/, reyn/）
    - path: /tmp/output
      scope: just_path
  mcp: [github, jira]         # スキルが呼び出せる MCP サーバー名のリスト
  python:
    - module: stats           # モジュール名（.py 拡張子なし）
      function: compute
      mode: safe              # safe | unsafe
    - module: rendering
      function: to_html
      mode: unsafe            # --allow-unsafe-python フラグが必要
  tool: [web_search]          # Control IR tool 名のリスト
  mcp_install: true           # mcp_install 操作を許可（省略可; デフォルト false）
  index_drop: true            # index_drop 操作を許可（省略可; デフォルト false）
  mcp_drop_server: true       # mcp_drop_server 操作を許可（省略可; デフォルト false）
```

### 主要フィールド

- **`shell`** — `true` または `false`（デフォルト `false`）。Control IR `shell` 操作の受け付け可否を制御します。CLI で `--allow-shell` も必要です。
- **`file.read`** / **`file.write`** — デフォルトゾーン外のパス。各エントリ: `path`（絶対パスまたは CWD 相対パス; `~` 展開あり）と `scope`（`just_path` または `recursive`）。`file.write` は `edit` および `delete` 操作も対象。省略するとデフォルト範囲内（read: CWD; write: `.reyn/`, `reyn/`）に留まります。
- **`mcp`** — スキルが呼び出せる MCP サーバー名のリスト。`reyn.yaml` の `mcp.servers` で定義されたサーバーキーのみ。
- **`python`** — preprocessor/postprocessor の `python` ステップで許可する Python 関数エントリのリスト。各エントリはステップで使用する `module` + `function` ペアと一致する必要があります。`mode: safe` はサンドボックス実行; `mode: unsafe` は CLI で `--allow-unsafe-python` が必要です。
- **`tool`** — スキルが呼び出せる Control IR tool 名のリスト（例: `web_search`, `web_fetch`）。
- **`mcp_install`** — `true` で `mcp_install` Control IR 操作を許可（デフォルト `false`）。
- **`index_drop`** — `true` で `index_drop` Control IR 操作を許可（デフォルト `false`）。
- **`mcp_drop_server`** — `true` で `mcp_drop_server` Control IR 操作を許可（デフォルト `false`）。

`permissions` ブロックは上限ゲートです: Phase の `allowed_ops` が許可する操作であっても、`skill.permissions` の範囲外であればディスパッチ時に拒否されます。レイヤー化された適用モデルについては [permission-model.md](../../concepts/permission-model.md) を参照してください。

## `required_credentials:` (省略可能)

- **型**: `list[str]`
- **デフォルト**: `["*"]`（完全委譲 — サブスキルが親のクレデンシャルをすべて継承）
- **目的**: このスキル（およびそれが呼び出すサブスキル）が `~/.reyn/secrets.env` および `~/.reyn/oauth_tokens.json` から読み取れるキーを宣言します。
- **適用**: `run_skill` の境界で OS がこのリストから `ScopedSecretStore` を構築し、親スキルのスコープと交差させます（parent-cap セマンティクス）。許可セット外の読み取りは `CredentialScopeError` を発生させます。

### 値

| 値 | 意味 |
|---|---|
| `[]` | クレデンシャル不要。シークレットを読み取らないスキルへの明示的な宣言として推奨。 |
| `["github_token", "openai_key"]` | 明示的な許可リスト — 指定したキーのみアクセス可能。 |
| `["*"]` | 完全委譲。信頼できる内部スキルにのみ使用してください。 |

### 監査

すべての `run_skill` 呼び出しは有効な `allowed_keys` を含む `sub_skill_credential_scope` イベントを発行します。[events.md](../runtime/events.md) を参照してください。

### 例

```yaml
---
name: pr-reviewer
entry: review
required_credentials:
  - github_token
permissions:
  mcp: [github]
  file.read:
    - path: .
      scope: recursive
final_output: review_output
---
```

### 関連情報

- [permission-model.md](../../concepts/permission-model.md) — "スキル別クレデンシャルスコーピング" セクション: 脅威モデルと詳細説明
- [secret-handling.md](../../concepts/secret-handling.md) — シークレットストアの概要
- [events.md](../runtime/events.md) — `sub_skill_credential_scope` イベントペイロード

## `postprocessor:`

スキルはオプションで `postprocessor` ブロックを宣言できます。これは Skill 完了時に LLM の最終出力と呼び出し元に返される artifact の間で実行される決定論的変換です。

```yaml
postprocessor:
  output_schema: rendered_post   # artifact 名文字列またはインライン dict
  output_description: |
    ワードカウント付きの完全にレンダリングされた HTML ポスト。
  steps:
    - type: python
      module: rendering
      function: to_html
      into: html_body
    - type: validate
      schema:
        type: object
        required: [html_body]
        properties:
          html_body: { type: string }
```

完全な構文（必須フィールド、省略可能フィールド、ステップ種別、`on_error` ポリシー、パーミッションゲート）については [postprocessor.md](postprocessor.md) を参照してください。設計の意図については [コンセプト: postprocessor](../../concepts/postprocessor.md) を参照してください。

## ボディ

frontmatter の後、Markdown ボディは Skill の散文による説明です: 何をするか、いつ使うか、例。`reyn skills <name>` で表示されます。

## バリデーション

`reyn lint <skill_name>` がチェックします:

- `graph` で参照されるすべての Phase が `phases/` に存在する。
- `entry` が `graph` のキーである。
- `final_output` が `artifacts/` または stdlib の artifact に一致する。
- Phase の artifact 参照が解決可能である。
- Python preprocessor ステップ（ある場合）が `permissions.python` に一致し、対応する `.py` ファイルが存在する。

## 例

```yaml
---
type: skill
name: my_explainer
description: Generate a one-paragraph explainer from a topic.
entry: outline
final_output: explainer
graph:
  outline: [expand]
  expand: [end]
---

# my_explainer

`topic_input` artifact を受け取り、フレンドリーで例豊富な
1 段落の説明文を生成します。2 つの Phase: `outline` が 3 つの
箇条書きを生成し、`expand` がそれらを散文に変換します。
```

## 関連情報

- [phase-md.md](phase-md.md) — Phase frontmatter
- `reference/dsl/artifact-yaml.md` — artifact スキーマファイル
- `reference/dsl/graph.md` — グラフセマンティクスの詳細
- [コンセプト: P2 Skill が構造を定義する](../../concepts/principles.md#p2-skill-defines-structure)
