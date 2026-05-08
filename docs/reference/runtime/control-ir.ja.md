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
| `shell` | シェルコマンドを実行する | `shell`（デフォルトオフ；`--allow-shell` が必要） |

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

## LLM に op が提示される場所

OS は利用可能な op をすべてのコンテキストフレームに `available_control_ops` として注入します。各エントリーは `kind`、一行の説明、動作例を含みます。LLM は意図を説明にマッピングして op を選択します。Phase の Markdown は op の構文を説明してはなりません（P8）。

## 関連情報

- [run.md](../cli/run.md) — `--allow-shell`、`--allow-untrusted-python`
- [events.md](events.md) — op の種類ごとに発行されるイベント
- [コンセプト: principles P8](../../concepts/principles.md#p8-phase-instructions-contain-only-domain-logic)
