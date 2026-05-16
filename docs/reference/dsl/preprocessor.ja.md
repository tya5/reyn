---
type: reference
topic: dsl
audience: [human, agent]
applies_to: [phases/*.md]
---

# Preprocessor

Phase は LLM が呼び出される**前**に実行される `preprocessor` チェーンを宣言できます。ステップは決定論的です: サブ Skill を呼び出す、リストに対して繰り返す、バリデーターを実行する、プランをリントする、または Python 関数を呼び出します。LLM はコンパイル時にスキーマが推論されたエンリッチされた入力 artifact を見ます。

## ステップの種類

| `type` | 目的 |
|--------|---------|
| `run_skill` | サブ Skill を呼び出し、その出力を名前付きキーに格納する |
| `iterate` | リストに対してサブステップを fan-out し、結果を収集する |
| `validate` | JSON Schema チェックを実行し、所見を LLM に提示する |
| `lint_plan` | プランの artifact に対して決定論的な構造チェックを実行する |
| `python` | ユーザーが提供した Python 関数を（サンドボックス内で）呼び出す |

すべてのステップは 2 つの共通のアイデアを持ちます:

- 結果は `into` で命名されたキーで入力 artifact に配置されます。
- ステップは順番に実行されます。各ステップは前のステップが生成したものを読み取れます。

## `run_skill`

```yaml
preprocessor:
  - run_skill:
      skill: recall_memory
      input:
        type: user_message
        data: { text: "what does the user prefer?" }
      into: relevant_memories
```

名前付きサブ Skill が完了まで実行されます。その `final_output` artifact が `input.relevant_memories` に格納されます。

## `iterate`

```yaml
preprocessor:
  - iterate:
      over: phase_eval_requests          # 入力内の配列へのドットパス
      apply:
        run_skill:
          skill: judge_phase
          input: { type: phase_eval_request, data: ${item} }
      into: phase_judgments
      on_error: fail                     # または "skip"
```

`over` の各アイテムが `apply` をトリガーします。結果は `into` にリストとして収集されます。MVP では内部ステップとして `run_skill` をサポートします。

## `validate`

```yaml
preprocessor:
  - validate:
      schema:
        type: object
        required: [topic]
        properties:
          topic: { type: string }
      target: input
      into: validation_findings
```

入力のターゲットスライスに対して JSON Schema バリデーションを実行します。所見（エラーと警告）が `into` に配置されます。LLM はその後、どのように反応するかを決定できます。

## `lint_plan`

プランの artifact に対して決定論的な構造チェック（サイクル検出、artifact カバレッジ）を実行します。`skill_builder` の `review_plan` Phase で使用されます。

## `python`

```yaml
preprocessor:
  - python:
      module: stats
      function: compute
      mode: safe                         # safe | unsafe
      output_schema:
        type: object
        required: [word_count]
        properties:
          word_count: { type: integer }
      into: stats
```

`<skill_dir>/<module>.py:<function>(artifact)` を呼び出し、JSON シリアライズ可能な結果を `into` に格納します。

### モード: `safe`

- 実行前に Reyn が AST 検証します: `open`、`eval`、`exec`、`__import__`、`compile`、`globals`、`locals`、`subprocess` およびその他の危険なモジュールを禁止。
- インポートはキュレートされた allowlist に制限されます（`math`、`statistics`、`json`、`re`、`random`、`time`、`datetime`、...）。プロジェクトは `reyn.yaml` の `python.allowed_modules` で拡張できます。
- 制限された `__builtins__`。
- クラッシュ分離とタイムアウトのためにサブプロセスで実行されます。

### モード: `unsafe`

- AST バリデーションなし。完全な Python。
- CLI で `--allow-unsafe-python` が必要、かつ `skill.md` の `permissions.python` エントリーに `mode: unsafe` の指定が必要です。
- safe モードが禁止するケイパビリティ（ファイル I/O、カスタムパッケージ）を必要とするステップにのみ使用します。

### `output_schema`

必須。LLM から見えるエンリッチメントの形状。コンパイル時にサンドボックスなしのユーザーコードを実行して推論しないため、明示的に宣言します。

## 共通ルール

- `into` キーは既存の入力 artifact キーと衝突してはなりません。
- ステップの順序が重要です: 後のステップは前のステップの `into` スロットを参照できます。
- リンターチェック: 各 `python` ステップのモジュール/関数は `permissions.python` エントリーに一致する必要があり、`.py` ファイルが存在し、関数が定義されており、（safe モードでは）AST が検証されます。

## Phase がやってはいけないこと

- **Phase のボディで preprocessor のメカニズムを説明する。** エンリッチされたフィールドは名前のみで参照してください。LLM はそれが preprocessor から来たことを知る必要はありません（P8）。

## 関連情報

- [phase-md.md](phase-md.md) — Phase frontmatter
- [リファレンス: permissions](../config/permissions.md) — `python` Permission の宣言
- [ハウツー: Python preprocessor を追加する](../../guide/for-skill-authors/add-a-python-preprocessor.md)
