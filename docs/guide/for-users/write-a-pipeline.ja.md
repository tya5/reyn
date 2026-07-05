# pipeline を書いて実行する

pipeline は、決定的なマルチステップ制御フローを記述する小さな YAML ファイルです。本ガイドでは、pipeline を書き、プロジェクトに配置し、起動するまでを扱います — さらに、エージェントがその場で生成する一回限りの手順向けに、登録不要な ad-hoc の代替手段も扱います。完全な文法と起動ツールのリファレンスは [Pipeline DSL リファレンス](../../reference/runtime/pipeline-dsl.ja.md) を、why / アーキテクチャは [Pipeline](../../concepts/runtime/pipelines.ja.md) を参照してください。

## 1. pipeline を書く

プロジェクトルートに `pipelines/` ディレクトリ(デフォルトのスキャン対象ディレクトリ)を作成し、`*.yaml` ファイルを配置します。以下は `name` を受け取り、挨拶し、その結果を叫ぶ pipeline です:

```yaml
# pipelines/greet.yaml
pipeline: greet
description: Greet a name and shout it.
steps:
  - transform: {value: "'Hello, ' + ctx.name + '!'", output: greeting}
  - shell: {command: !expr "'echo ' + greeting", output: shouted}
```

このファイルについて注目すべき点がいくつかあります:

- pipeline は `pipeline:` キーの名前(`greet`)の下に登録されます。ファイル名ではありません — `pipelines/greet.yaml` は何にリネームしても `greet` として登録され続けます。
- 各ステップは、自身の種別(`transform`、`tool`、`agent`、または合成プリミティブ(Pipeline DSL リファレンス参照)のいずれか)を名前とする単一キーのマッピングです。
- `ctx.name` はこの pipeline が期待する seed input です。`greeting` は最初のステップの `output` で書き込まれた後、後続のステップで `ctx.greeting` として利用可能になります — ここでは 2 番目のステップのコンテキストがそれを named store として公開しているため、`!expr` 文字列連結の中で bare な `greeting` として参照しています。
- `!expr` は `command` をリテラル文字列ではなく評価すべき expression としてマークします — [リテラル vs `!expr`](../../reference/runtime/pipeline-dsl.ja.md#vs-expr) 参照。
- `shell` は operator のサンドボックスの中でコマンドを実行し、前のステップの pipe data を JSON エンコードして STDIN に渡します — この pipeline はその入力を使いませんが、完全な STDIN/STDOUT の契約は[リファレンスの `shell` セクション](../../reference/runtime/pipeline-dsl.ja.md#tool-shell)を参照してください。

## 2. セッションを開始(または再起動)する

pipeline はセッション開始時にディスクから登録されます — 別途「インストール」ステップは無く、デフォルトの `pipelines/` ディレクトリには `reyn.yaml` へのエントリも不要です。セッションを再起動する(または新しく開始する)と `greet` が登録されます。

ファイルの parse に失敗した場合、または 2 つのファイルが同じ `pipeline:` 名を宣言した場合、セッション開始は問題のファイルを名指しして大きく失敗します — タイプミスが、出荷するつもりだった pipeline を静かに消すことはありません。詳細な表は [Pipeline registration § Failure behavior](../../concepts/runtime/pipeline-registration.md#failure-behavior-fail-loud) を参照してください。

## 3. 起動する

エージェントは通常のツール呼び出しで `greet` を起動できます:

```
run_pipeline(name="greet", input={name: "Reyn"})
```

または、登録済みのすべての pipeline についてアクションカタログが提示する qualified なカタログ verb で:

```
pipeline__greet({name: "Reyn"})
```

どちらも pipeline が完了するまで block し、最終出力(ここでは叫ばれた挨拶)を返します。run の間、ライブなステップ進捗が TUI で見え、Ctrl-C は途中で kill するのではなく次のステップ境界でクリーンに停止させます。

### 同期 vs 非同期

手順が長時間実行され、それを待って block したくない場合は、非同期形式を使います:

```
run_pipeline_async(name="greet", input={name: "Reyn"})
```

これは即座に `{status: "started", run_id: "..."}` を返します。結果は後で会話の中に `[pipeline]` メッセージとして届きます。結果をインラインで受け取りたく、待つのが問題なければ `run_pipeline` を、fire-and-forget な起動には `run_pipeline_async` を使ってください。どちらも同等に crash-recoverable です — run の途中でのプロセス再起動は、完了済みステップを再実行するのではなく、中断した箇所からちょうど resume します([Pipeline § Crash recovery](../../concepts/runtime/pipelines.ja.md#crash-recovery)参照)。

## 4. Ad-hoc な、登録不要の代替手段

手順が一回限りのこともあります — crash-recovery と構造的な安全性のために pipeline として書く価値はあるが、ファイルとして登録する価値は無い場合です。`run_pipeline_inline`(とその非同期版 `run_pipeline_inline_async`)は `pipeline:` ドキュメントと同じ DSL を受け取りますが、呼び出し時にエージェントが生成する文字列としてです:

```
run_pipeline_inline(
  definition="""
    pipeline: adhoc_greet
    steps:
      - transform: {value: "'Hi, ' + ctx.name", output: greeting}
  """,
  input={name: "Reyn"},
)
```

定義は parse され、静的解析ゲートを通されます — schema 参照が解決すること、ツール名が解決すること、どのステップも別の pipeline を起動したり delegate したりしないこと、`agent` ステップが起動者自身の identity の下でのみ実行されること — **何かが spawn される前に**。不正な定義は明確に失敗し何も spawn しません。良い定義は、登録済み pipeline と全く同様に crash-recoverable です。その完全な定義が run 自身の recovery 状態と共に移動するためです。完全なゲートのチェックリストは [Ad-hoc inline 起動](../../reference/runtime/pipeline-dsl.ja.md#ad-hoc-inline) を参照してください。

## 実践例: fan out してから merge する

`for_each` と `match` を使った、もう少し大きな例です — 複数のレビュアーで文書を並行レビューし、全員が合意したかどうかで分岐します:

```yaml
# pipelines/review.yaml
pipeline: review
description: Fan a document out to reviewers, then branch on the verdict.
steps:
  - for_each:
      over: ctx.reviewers
      max_parallel: 4
      on_error: "retry(1)"
      do:
        agent:
          prompt: "Review this document as {item}: {ctx.doc}. Reply with passed (bool) and notes (string)."
          schema: Review
      collect: {transform: {value: "pipe"}}
      output: reviews
  - transform: {value: "all(reviews, r -> r.passed)", output: all_passed}
  - match:
      on: all_passed
      cases:
        "True": {pipeline: report_pass, pass: [reviews]}
        "False": {pipeline: report_fail, pass: [reviews]}
      output: report
---
schema: Review
fields:
  passed: {type: bool}
  notes: {type: string}
```

レビュアーの identity のリストと文書を渡して起動します:

```
run_pipeline(name="review", input={reviewers: ["reviewer_a", "reviewer_b"], doc: "..."})
```

各レビュアーは隔離された並行 `agent` ステップとして実行され(最大 4 並行、失敗時は 1 回リトライ)、全員の結果が揃うと、R1 の `all()` コンビネータで `all_passed` という 1 つの boolean に畳み込まれ、`match` がそれに応じて `report_pass` または `report_fail` の sub-pipeline にルーティングします(どちらも別途登録が必要です。単一ファイルで完結させたい場合は、素の `transform`/`tool` ステップに置き換えてください)。
