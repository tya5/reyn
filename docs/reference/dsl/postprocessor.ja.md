---
type: reference
topic: dsl
audience: [human, agent]
applies_to: [skill.md]
---

# Postprocessor

Skill は `postprocessor` ブロックを宣言できます。このブロックは LLM が finish artifact を発行した**後**、その artifact が呼び出し元に返される前に実行されます。ステップは決定論的です。サブ Skill を呼び出す、リストに対して繰り返す、バリデーターを実行する、プランをリントする、または Python 関数を呼び出します。呼び出し元が受け取るのは Postprocessor の出力であり、生の LLM artifact ではありません。

**なぜ** Postprocessor を使うのかについての解説と実例は [Concepts: postprocessor](../../concepts/postprocessor.md) を参照してください。

## ブロックの位置

`postprocessor:` は `skill.md` frontmatter の中に、`final_output:`、`graph:`、`permissions:` の兄弟として宣言します。

```yaml
---
type: skill
name: blog_writer
entry: draft
final_output: post            # LLM contract — LLM が生成しなければならないもの
graph:
  draft: [review]
  review: [end]
permissions:
  python:
    - module: rendering
      function: to_html
      mode: pure
    - module: rendering
      function: count_words
      mode: pure
postprocessor:
  output_schema: rendered_post  # 呼び出し元 contract — Skill が返すもの
  output_description: |
    Fully rendered HTML post with word count and reading time.
  steps:
    - type: python
      module: rendering
      function: to_html
      into: html_body
    - type: python
      module: rendering
      function: count_words
      into: word_count
---
```

## 必須フィールド

### `output_schema`

Postprocessor が生成する artifact の schema を宣言します。以下のどちらかを指定できます。

- **artifact 名の文字列** — Skill の `artifacts/` ディレクトリまたは stdlib で定義された artifact を参照します。Skill 間での再利用に推奨されます。

  ```yaml
  postprocessor:
    output_schema: rendered_post
  ```

- **インライン dict** — frontmatter に直接宣言した JSON Schema の dict リテラル。

  ```yaml
  postprocessor:
    output_schema:
      type: object
      required: [html_body, word_count]
      properties:
        html_body:   { type: string }
        word_count:  { type: integer, minimum: 1 }
  ```

OS は Postprocessor の出力をこの schema に対して validate します。バリデーション失敗は失敗したステップの `on_error` ポリシーをトリガーします（ポリシーが設定されていない場合は Skill を中断します）。

## 省略可能なフィールド

### `output_name`

生成された artifact の短い識別子。イベントペイロードとログ行に使用されます。省略した場合はデフォルトで Skill 名に `_post` サフィックスが付きます。

```yaml
postprocessor:
  output_schema: rendered_post
  output_name: rendered_post
```

### `output_description`

Postprocessor の出力の詳細な説明。`reyn skills <name>` でスキルの本文と並んで表示されます。

### `steps`

決定論的なステップの順序付きリスト。ステップは順番に実行されます。各ステップは LLM finish artifact と、前のステップが生成した `into` キーを読み取れます。Preprocessor と同じステップ種別をサポートしています — 各構文の詳細は [preprocessor.md](preprocessor.md) を参照してください。

| `type` | 用途 |
|--------|------|
| `run_skill` | サブ Skill を呼び出し、その出力を名前付きキーに格納する |
| `iterate` | サブステップをリストに対してファンアウトし、結果を収集する |
| `validate` | 累積した artifact に対して JSON Schema チェックを実行する |
| `lint_plan` | プランの artifact に対して決定論的な構造チェックを実行する |
| `python` | ユーザー提供の Python 関数を（サンドボックス内で）呼び出す |

`steps` を省略した場合、Postprocessor は validate のみの変換として動作します。LLM artifact が `output_schema` に対して validate され、成功するとそのまま返されます。

## `on_error` ポリシー

各ステップには `on_error: fail | skip | empty` を宣言できます。セマンティクスは Preprocessor と同一です。

| 値 | 動作 |
|-------|------|
| `fail`（デフォルト） | ステップの失敗は例外を発生させ Skill を中断します。中断は `WorkflowAbortedError` です。Skill ごとの snapshot は削除されます（自動再開なし）。 |
| `skip` | ステップの失敗はログに記録され、後続ステップはそのステップの `into` キーなしで続行します。 |
| `empty` | ステップの失敗はそのステップの `into` キーに空の結果を生成し、後続ステップは続行します。 |

呼び出し元が不正な artifact を受け取らないよう、デフォルトは `fail` にしてください。あると便利だが呼び出し元の contract に必須でないエンリッチメントにのみ `skip` または `empty` を使用してください。

## 実行可能 op セットとパーミッション

実行可能 op セットは Preprocessor と完全に同一です。

- `run_skill` は許可されます。
- `ask_user` は **禁止** です（Skill の finish は呼び出し元同期のため、この時点でのユーザーインタラクションは未定義です）。
- LLM ステップはありません — Postprocessor は定義上、決定論的です。

op セットの詳細な説明は [preprocessor.md](preprocessor.md) を参照してください。

パーミッション強制は `skill.permissions` を使用します — `skill.md` frontmatter の Skill レベルの宣言です。Postprocessor ステップに対する Phase レベルのパーミッションゲートはありません。セマンティクスは [permission-model.md](../../concepts/permission-model.md) を参照してください。

## Resume 統合

Postprocessor のステップは Preprocessor および Phase の op と同じ `dispatch_tool` を通じて実行されます。各ステップは `step_completed` イベントを発行し、メモ化に参加します。Postprocessor の途中でクラッシュした場合:

1. Skill ごとの snapshot が `current_phase = "__post__"` を記録します（予約済みの擬似 Phase）。
2. 自動再開は最初の未コミットステップから Postprocessor をリプレイし、メモ参照によって既にコミット済みのステップをスキップします。
3. World-purity op は再開時に再実行されます（ADR-0011 参照）。

LLM の finish artifact は Postprocessor 開始前に Workspace に永続化されるため、インプロセス状態が失われても再開時に耐久性のある入力 artifact が確保されます。Postprocessor ステップの op 呼び出し ID は `__post__.<step_idx>` のパターンに従います（例: `__post__.0`、`__post__.1`）。より広い再開の仕組みについては [skill-resume.md](../../concepts/skill-resume.md) を参照してください。

## 実例

### 1. Python エンリッチメントを伴うインライン `output_schema`

```yaml
postprocessor:
  output_schema:
    type: object
    required: [title, body, word_count]
    properties:
      title:       { type: string }
      body:        { type: string }
      word_count:  { type: integer, minimum: 1 }
  output_description: Draft post enriched with word count.
  steps:
    - type: python
      module: stats
      function: count_words
      mode: pure
      output_schema:
        type: object
        required: [word_count]
        properties:
          word_count: { type: integer }
      into: word_count
```

`stats.py` 関数は LLM の finish artifact を受け取り `{ word_count: 847 }` を返します。OS は `word_count` を artifact にマージし、`output_schema` に対して validate します。

対応する `permissions.python` エントリーが必要です。

```yaml
permissions:
  python:
    - module: stats
      function: count_words
      mode: pure
```

### 2. `output_schema` に artifact 名参照を使う

```yaml
postprocessor:
  output_schema: code_review_enriched   # artifacts/code_review_enriched.yaml で定義
  steps:
    - type: run_skill
      skill: resolve_owners
      input:
        type: affected_files_list
        data: { files: "${artifact.affected_files}" }
      into: tagged_owners
```

artifact 名の形式では schema の所有権を `artifacts/code_review_enriched.yaml` に委譲するため、そこでバージョン管理でき、複数の Skill をまたいで再利用できます。

### 3. validate のみの Postprocessor（ステップなし）

```yaml
postprocessor:
  output_schema:
    type: object
    required: [summary, severity]
    properties:
      summary:  { type: string, minLength: 10 }
      severity: { type: string, enum: [low, medium, high, critical] }
```

`steps` キーなし。OS は LLM の finish artifact を `output_schema` に対して validate し、成功するとそのまま返します。これが最も軽量な使い方で、変換なしに `final_output` より厳格な形状を強制します。

## 関連情報

- [Concepts: postprocessor](../../concepts/postprocessor.md) — 解説と使い所
- [preprocessor.md](preprocessor.md) — ステップ型（Postprocessor と共通）
- [skill-md.md](skill-md.md) — Skill frontmatter の完全リファレンス
- [permission-model.md](../../concepts/permission-model.md) — `skill.permissions` のセマンティクス
- [skill-resume.md](../../concepts/skill-resume.md) — Postprocessor が統合する再開の仕組み
