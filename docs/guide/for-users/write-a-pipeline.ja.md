# pipeline を書いて実行する

pipeline は、決定的なマルチステップ制御フローを記述する小さな YAML ファイルです。本ガイドでは、pipeline を書き、登録し、起動するまでを扱います — さらに、エージェントがその場で生成する一回限りの手順向けに、登録不要な ad-hoc の代替手段も扱います。完全な文法と起動ツールのリファレンスは [Pipeline DSL リファレンス](../../reference/runtime/pipeline-dsl.ja.md) を、why / アーキテクチャは [Pipeline](../../concepts/runtime/pipelines.ja.md) を参照してください。

## 1. pipeline を書く

プロジェクト内の任意の場所に Appendix-B DSL ファイルを書きます(デフォルトのスキャン対象ディレクトリは無くなりました — 手順 2 参照)。以下は `name` を受け取り、挨拶し、その結果を叫ぶ pipeline です:

```yaml
# pipelines/greet.yaml
pipeline: greet
description: Greet a name and shout it.
steps:
  - transform: {value: "'Hello, ' + ctx.name + '!'", output: greeting}
  - tool: {name: sandboxed_exec, args: {argv: !expr "['echo', ctx.greeting]"}, output: shouted}
```

このファイルについて注目すべき点がいくつかあります:

- pipeline は `pipeline:` キーの名前(`greet`)の下に登録されます。ファイル名ではありません — `pipelines/greet.yaml` は何にリネームしても `greet` として登録され続けます。
- 各ステップは、自身の種別(`transform`、`tool`、`agent`、または合成プリミティブ(Pipeline DSL リファレンス参照)のいずれか)を名前とする単一キーのマッピングです。
- `ctx.name` はこの pipeline が期待する seed input です。`greeting` は最初のステップの `output` で書き込まれた後、それ以降のすべてのステップから `ctx.greeting` として利用可能になります。bare name のショートカットはありません — `ctx.greeting` の代わりに bare な `greeting` として読もうとするとステップが失敗します。すべての expression は `ctx`(すべての named store)と `pipe`(直前のステップ自身の結果)の 2 つだけをトップレベルキーとして持つコンテキストに対して評価されるためです。完全なルールと実例のトレースは [ステップ間のデータフロー](../../reference/runtime/pipeline-dsl.ja.md#data-flow-between-steps) を参照してください。
- `!expr` は `argv` をリテラルのリストではなく評価すべき expression としてマークします — [リテラル vs `!expr`](../../reference/runtime/pipeline-dsl.ja.md#vs-expr) 参照。
- `sandboxed_exec` は operator のサンドボックスの中で `argv` を実行します(argv のみ — シェル解釈はありません)。前のステップの pipe data は `stdin_pipe: !expr pipe` 引数で STDIN に渡せます — この pipeline はその入力を使いませんが、完全な STDIN/STDOUT の契約は[リファレンスの `sandboxed_exec` ステップの説明](../../reference/runtime/pipeline-dsl.ja.md#tool-step-results)を参照してください。

## 2. 登録する

pipeline は、config 内の明示的な `pipelines.entries` 宣言によってのみ登録されます — ディレクトリスキャンは無いため、ディスク上に置かれた `*.yaml` ファイルは登録されるまでどのセッションからも不可視です。`reyn.yaml` にエントリを追加します(エントリキーは DSL 自身が宣言する `pipeline:` 名と完全に一致しなければなりません):

```yaml
# reyn.yaml
pipelines:
  entries:
    greet:
      path: pipelines/greet.yaml
      description: "Greet a name and shout it"
```

あるいは、同等の効果として、エージェントに `pipeline_management__install_local(path="pipelines/greet.yaml")` を呼んでもらうこともできます — これはファイルを parse し、名前を検証し、`.reyn/config/pipelines.yaml` に同種のエントリを書き込みます。どちらの方法でも、変更は次のターン境界で hot-reload によって反映されます — 新しく登録した pipeline を反映させるのに**セッションの再起動は不要**です。

ファイルの parse に失敗した場合、または 2 つのエントリが同じ `pipeline:` 名を宣言した場合、読み込みは問題のエントリを名指しして大きく失敗します — タイプミスが、出荷するつもりだった pipeline を静かに消すことはありません。詳細な表は [Pipeline registration § Failure behavior](../../concepts/runtime/pipeline-registration.md#failure-behavior-fail-loud) を参照してください。

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
  - transform: {value: "all(ctx.reviews, r -> r.passed)", output: all_passed}
  - match:
      on: ctx.all_passed
      cases:
        "True": {pipeline: report_pass, pass: {reviews: ctx.reviews}}
        "False": {pipeline: report_fail, pass: {reviews: ctx.reviews}}
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

各レビュアーは隔離された並行 `agent` ステップとして実行され(最大 4 並行、失敗時は 1 回リトライ)、全員の結果が揃うと、R1 の `all()` コンビネータで `all_passed` という 1 つの boolean に畳み込まれます — これは bare な `reviews` ではなく、`for_each` ステップの `output` が書き込んだ永続的な named store である `ctx.reviews` から読みます — そして `match` がそれに応じて `report_pass` または `report_fail` の sub-pipeline にルーティングします(どちらも別途登録が必要です。単一ファイルで完結させたい場合は、素の `transform`/`tool` ステップに置き換えてください)。

この 2 つ目の pipeline も、手順 2 と同様に独自の `pipelines.entries` 宣言(または `pipeline_management__install_local` 呼び出し)が必要です — エージェントがそれを起動できるようになる前に。

## 5. CLI から直接 pipeline を管理・実行する

ここまでは、すべてチャットセッション内のエージェントのツール呼び出しを通じて行われていました。`reyn pipe` は、list / install / run という同じ 3 つのことを、ライブセッションなしで pipeline を管理・実行したい場合のために、直接の CLI コマンドとして提供します:

```console
$ reyn pipe list
No pipelines configured.
Add one with: reyn pipe install --path <file.yaml>  or edit reyn.yaml manually.

$ reyn pipe install --path pipelines/greet.yaml --non-interactive
Installing pipeline from path: pipelines/greet.yaml

Pipeline 'greet' installed successfully.
Config written to: .reyn/config/pipelines.yaml
...

$ reyn pipe list
NAME         PATH                      DESCRIPTION                  ENABLED  LOAD STATUS
──────────────────────────────────────────────────────────────────────────────────────
greet.greet  pipelines/greet.yaml      Greet a name and shout it    yes      loaded

$ reyn pipe run greet.greet --input '{"name": "Reyn"}'
{
  "pipe_data": "Hello, Reyn! (shouted)",
  "named_stores": {
    "name": "Reyn",
    "greeting": "Hello, Reyn!",
    "shouted": "Hello, Reyn! (shouted)"
  }
}
```

`reyn pipe install` は `--source <git/GitHub URL>` (`reyn mcp install` と同じ `//subdir` 記法) と、インストールする pipeline の identity を事前に明示する `--name` も受け付けます — DSL 自身が宣言する `pipeline:` 名と食い違う場合は、両者を静かに乖離させるのではなく、明確なエラーで拒否されます。

`reyn pipe list` の **NAME** 列は、`loaded` エントリについては常に実行可能な名前(=`reyn pipe run` がそのまま受け付ける名前)そのものを表示します — ここに表示されたものはそのまま `reyn pipe run` に渡せます。`reyn pipe run` は、bare な entry-key(`greet.greet` の代わりに `greet`)がちょうど 1 つの登録済み pipeline に一意に一致する場合はそれも受け付け、`note: resolved '<key>' -> '<full-name>'` を stderr に出力して解決内容を明示します。key が複数の pipeline を登録している場合は一意に決まらないため、`run` は候補を列挙してエラーにします。

`reyn pipe list` の **LOAD STATUS** 列は、ログを掘らずに壊れたエントリを直接見る手段です: `enabled: true` なのにパースに失敗したエントリ(DSL の不備、ファイル欠如、宣言名の重複)は(何も実行可能な形で登録されなかったため entry-key のまま)、どこにも現れず静かに消えるのではなく、その場で `FAILED` と表示されます。

`reyn pipe run` は pipeline を **CLI プロセス自身の中で単独実行**します — `tool:` / `agent:` を含む、すべてのステップ種別が実行できます。`tool:` ステップは、ライブセッションの `tool` ステップと同じ実際のツール実行経路を通して(スタンドアロンな)ディスパッチされます。`agent:` ステップは `default` エージェントの下で本物の短命("ephemeral")セッションを spawn し、チャットセッションの `agent:` ステップと同様に最後まで実行します。`reyn pipe run` の背後にはライブなチャット REPL も `--docker`/`--sandbox-backend` によるコンテナオプションもありません(ホストのファイルシステム/実行のみ)— それらが必要な pipeline は `reyn chat`/`reyn run` から実行してください。また `reyn pipe run` の呼び出しにはクラッシュリカバリもありません: これは one-shot のフォアグラウンドコマンドなので、途中で kill/中断された実行は、他の CLI ツールと同様、単に失敗したコマンドであり、再開可能な driver-session ではありません。
