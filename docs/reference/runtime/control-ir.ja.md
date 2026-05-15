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
| `file` | ファイルの読み取り、書き込み、glob、grep、編集、削除 | `file.<op>` |
| `ask_user` | Phase を一時停止してユーザーに質問する | なし（常に許可） |
| `run_skill` | 別の Skill をサブワークフローとして実行する | なし（Skill レベルの決定） |
| `lint` | Skill ディレクトリに DSL リンターを実行する | なし |
| `shell` | シェルコマンドを実行する（**非推奨** — `sandboxed_exec` を使用、FP-0017） | `shell`（デフォルトオフ；`--allow-shell` が必要） |
| `sandboxed_exec` | `SandboxPolicy` と `SandboxBackend` を介して argv を実行する（FP-0017） | バックエンドが強制（`SandboxPolicy`） |
| `web_search` | DuckDuckGo で公開ウェブを検索する | Tier 1 — デフォルト許可；`reyn.yaml` の `web.search: deny` でブロック |
| `web_fetch` | 単一 URL を取得してテキストを抽出する | Tier 1 — デフォルト許可；`reyn.yaml` の `web.fetch: deny` でブロック |
| `mcp` | 設定済み MCP server のツールを呼び出す | Skill frontmatter の `permissions.mcp: [server_name]` |

## 共通エンベロープ

すべての op は `kind` ディスクリミネーターを持つ JSON オブジェクトです:

```json
{
  "kind": "file",
  "op": "read",
  "path": "src/foo.py"
}
```

OS は op をその kind のスキーマに対して検証し、実行し、呼び出し元 Phase に結果を返します。

## `file`

サブ操作: `read`、`write`、`edit`、`delete`、`glob`、`grep`。

```json
{"kind": "file", "op": "read", "path": "src/foo.py"}

{"kind": "file", "op": "write", "path": "out.txt", "content": "..."}

{"kind": "file", "op": "edit", "path": "src/foo.py",
 "old_string": "...", "new_string": "..."}

{"kind": "file", "op": "delete", "path": "tmp.txt"}

{"kind": "file", "op": "glob", "pattern": "**/*.py"}

{"kind": "file", "op": "grep", "path": "src", "pattern": "def \\w+",
 "glob": "**/*.py", "output_mode": "content"}
```

Permission スコープは op の種類ごとに設定されます。`reference/config/permissions.md` を参照してください。

## `ask_user`

Phase を一時停止してユーザーに質問します。OS は質問を表示し、stdin を読み取り、回答を `user_message` artifact として入力にマージした上で**同じ Phase** を再実行します。訪問カウントは増加しません。

```json
{
  "kind": "ask_user",
  "question": "どのモデルをターゲットにしますか？",
  "suggestions": ["light", "standard", "strong"]
}
```

## `run_skill`

別の Skill をサブワークフローとして実行します。結果は呼び出し元 Phase が使用するための構造化 artifact として返されます。

```json
{
  "kind": "run_skill",
  "skill": "recall_memory",
  "input": {"type": "user_message", "data": {"text": "what did I tell you about my preferences?"}}
}
```

LLM 駆動ではなく Phase の preprocessor から決定論的に呼び出す場合は、`run_skill` preprocessor ステップを使用してください。`reference/dsl/preprocessor.md` を参照してください。

## `lint`

Skill ディレクトリに DSL リンターを実行します。Skill を構築する Skill（`skill_builder`、`skill_improver`）が出力を検証するために使用します。

```json
{
  "kind": "lint",
  "skill_path": "reyn/local/my_skill"
}
```

## `shell`

シェルコマンドを実行します。**デフォルトオフ。** ランタイムを `--allow-shell` で起動しなければならず、かつプロジェクトが `reyn.yaml` で `shell` を許可している（またはプロンプト経由でランごとに付与している）必要があります。

```json
{
  "kind": "shell",
  "cmd": "reyn run my_skill 'hello'",
  "timeout": 120
}
```

シェルが拒否された場合、OS は `shell_not_allowed` を発行し、Phase を失敗させるのではなく拒否結果を返します。

**FP-0017 により非推奨。** 1.0 で削除予定。代わりに `sandboxed_exec`（下記）を使用してください — 宣言された `SandboxPolicy` を強制する `SandboxBackend` を経由します。スキル初回呼び出し時に `DeprecationWarning` が発行されます。

## `sandboxed_exec`

宣言された `SandboxPolicy` と OS が選択した `SandboxBackend` を介して `argv` を実行します（FP-0017）。分離強制が必要な（または将来必要になる）ケースで `shell` を置き換えます。

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
- `allow_subprocess`（省略可、デフォルト `false`）— 子プロセス生成の許可。
- `env_passthrough`（省略可）— 引き渡す環境変数名（それ以外は除去）。
- `timeout_seconds`（省略可、デフォルト `60`）— ウォールクロック上限。

**バックエンド選択**: `get_default_backend()` がプラットフォームに応じて選択します。macOS < 26 では `SeatbeltBackend`（sandbox-exec SBPL）。Linux ≥ 5.13 かつ `sandbox-linux` extra インストール済みの場合は `LandlockBackend`（+ オプションの seccomp-BPF スタック）。その他のプラットフォームまたは選択バックエンドが利用不可の場合は `NoopBackend`（監査のみ、強制なし）にフォールバック — 初回使用時に一行 WARN を出力。`reyn.yaml` の `sandbox.backend`（`auto` | `seatbelt` | `landlock` | `noop`）および `sandbox.on_unsupported`（`warn` | `error` | `ignore`）で上書き可能。

結果フィールド: `returncode`、`stdout`、`stderr`、`truncated`、`backend`。

発行イベント: `sandboxed_exec_started`、`sandboxed_exec_completed`（P6 監査証跡）。

## `web_search`

DuckDuckGo を使って公開ウェブを検索し、構造化された結果を返します。**Tier 1** — デフォルト許可；Permission 宣言不要（FP-0022）。`reyn.yaml` の `web.search: deny` でプロジェクト全体をブロックできます。

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

単一 URL を取得し、テキスト抽出したコンテンツを返します。**Tier 1** — デフォルト許可；Permission 宣言不要（FP-0022）。通常は `web_search` の後、特定の結果ページを詳しく読むために使用します。`reyn.yaml` の `web.fetch: deny` でブロック、`web.fetch: allow` で明示的に事前承認できます。

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

OS は server のトランスポート（`stdio`、`http`、`sse`）を解決し、`MCPClient` 経由でディスパッチして、ツール結果を返します。呼び出しごとに `mcp_called`、`mcp_completed`、（失敗時）`mcp_failed` イベントが発行されます。

server の設定、トランスポートの選択、セキュリティモデルについては [concepts/mcp.md](../../concepts/mcp.md) を参照してください。

## `judge_output`

Phase 内の評価ループで使用する LLM ベースの出力スコアラー（FP-0007 Component D）。`target` の dot-path で値を解決し、呼び出し元が供給する `rubric` と共に LLM を呼び出し、スコア（0.0〜1.0）と合格/不合格フラグを返します。

```json
{
  "kind": "judge_output",
  "target": "artifact.data.summary",
  "rubric": "0.0〜1.0 でスコアリング: サマリーは簡潔で正確かつ完全ですか？",
  "threshold": 0.8,
  "on_fail": "transition"
}
```

フィールド:
- `target`（str、必須）: スコアリング対象の値への dot-path（例: `"artifact.data.summary"`）。現在のワークスペース artifact に対して解決されます。
- `rubric`（str、必須）: LLM prompt 本文。評価基準は Skill author が記述します。OS はこの内容を解釈しません（P7）。
- `threshold`（float、省略可、デフォルト `0.8`）: 合格スコア（`[0.0, 1.0]`）。
- `on_fail`（`"transition" | "abort" | "continue"`、省略可、デフォルト `"transition"`）:
  - `"transition"`: LLM が次の Phase を選択（既存の decision フロー）。
  - `"abort"`: Skill 実行を中止。
  - `"continue"`: スコアを記録するのみ。フロー変更なし。
- `model`（str | null、省略可）: モデルクラスのオーバーライド（例: `"strong"`）。省略時は Skill の現在のモデルを使用。

戻り値: `{"kind": "judge_output", "score": float, "passed": bool, "reason": str, "threshold": float, "on_fail": str}`

Audit イベント: `tool_executed`（`op=judge_output, target, score, passed, threshold, reason`）（P6）。

**P7 注記**: Reyn は rubric に依存しません。rubric の内容は Skill author の authored prompt の一部であり、OS は解釈せずそのまま LLM に渡すだけです。

## `skill_resolve`

Skill 名をオンディスクの `skill.md` パスに解決します。標準解決チェーン（`reyn/local/` → `reyn/project/` → `stdlib/`）を使用し、パスメタデータを返します。ファイル内容の読み取りは行いません。

```json
{
  "kind": "skill_resolve",
  "name": "skill_improver"
}
```

フィールド:
- `name`（str、必須）: 短い Skill 名（スラッシュや `.md` 拡張子は不要）。

戻り値:
- `name: str` — 入力のエコー
- `resolved: bool` — いずれかの解決レイヤーに `skill.md` が存在する場合 `true`
- `skill_md_path: str | null` — `skill.md` への絶対パス。未解決の場合 `null`
- `source: "local" | "project" | "stdlib" | null` — マッチした解決レイヤー
- `skill_dir: str | null` — `skill.md` の親ディレクトリ。未解決の場合 `null`

**イベント**: `skill_resolve_completed`（`name`、`resolved`、`source`）— 呼び出しごとに発行（P6）。

**パーミッション**: 不要。本 op は読み取り専用（信頼済み解決チェーン内のパス存在確認のみ）であり、ファイル内容は読み取りません。

**OpPurity**: `world`（ファイルシステムメタデータの読み取り。Skill が追加／削除されると結果が変わる可能性あり）。

**ユースケース**: Skill の絶対パスを必要とする stdlib python ステップは、このフィルシステムウォーク処理を本 op に委ねることで `mode: safe` を宣言できます。R-PURE-MODE Class D リファクタの主要利用元は `skill_improver/copy_to_work_resolver` および `eval_builder/analyze_skill_resolver` です。

---

**コントリビューター向けメモ:** `src/reyn/schemas/models.py` および `src/reyn/op_runtime/registry.py` に新しい Control IR op kind を追加する際は、**同じ PR でここにセクションを追加してください**。reference と registry は同期を保つ必要があります。ルールの詳細は [CLAUDE.md](https://github.com/tya5/reyn/blob/main/CLAUDE.md) を参照してください。

## LLM に op が提示される場所

OS は利用可能な op をすべてのコンテキストフレームに `available_control_ops` として注入します。各エントリーは `kind`、一行の説明、動作例を含みます。LLM は意図を説明にマッピングして op を選択します。Phase の Markdown は op の構文を説明してはなりません（P8）。

## 関連情報

- [run.md](../cli/run.md) — `--allow-shell`、`--allow-untrusted-python`
- [events.md](events.md) — op の種類ごとに発行されるイベント
- [コンセプト: principles P8](../../concepts/principles.md#p8-phase-instructions-contain-only-domain-logic)
