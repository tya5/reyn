---
type: reference
topic: runtime
audience: [human, agent]
search_hints: [pipeline DSL, pipeline grammar, EBNF, formal grammar, agent generation grammar, transform step, tool step, agent step, call step, match step, fold step, for_each step, parallel step, expr, R1 expression, verify schema, run_pipeline, run_pipeline_async, run_pipeline_inline, safety.spawn.max_pipeline_fan_out_depth, safety.spawn.max_pipeline_spawns, parse_json]
---

# Pipeline DSL リファレンス

pipeline 定義の規範的な文法 — ステップ種別、合成プリミティブ、それらが評価される expression 言語、schema / `verify: schema` の仕組み、そして pipeline を起動する 4 つのツール。why / アーキテクチャは [Pipeline](../../concepts/runtime/pipelines.ja.md) を、定義がセッションにどう届くかは [Pipeline registration](../../concepts/runtime/pipeline-registration.md) を参照してください。

## ドキュメントの形

pipeline 定義は `---` で区切られた 1 つ以上の YAML ドキュメントです:

- `pipeline:` ドキュメントちょうど 1 つ — pipeline 本体。
- `schema:` ドキュメント 0 個以上 — pipeline のステップが `verify: schema` で参照できる、名前付き schema([Schema](#schema-verify-schema) 参照)。

```yaml
schema: Review
fields:
  passed: {type: bool}
  notes: {type: string}
---
pipeline: review_and_report
description: Review a document and summarize the verdict.
steps:
  - agent: {prompt: "Review {ctx.doc}. Reply with passed/notes.", schema: Review, output: review}
  - transform: {value: "ctx.review.passed and 'OK' or 'NEEDS WORK'", output: verdict}
```

## ステップ間のデータフロー {#data-flow-between-steps}

すべてのステップの expression / テンプレートは、常にこの 2 つに対してのみ評価されます — 第三の形は無く、bare name のショートカットもありません:

- **`ctx`** — 現在のスコープでこれまでに蓄積された、すべての named store をフラットな namespace として持つもの。`output: X` を宣言したステップは、それ以降のスコープ内のすべてのステップから `ctx.X` として読めるようになります — bare な `X` としてではありません。
- **`pipe`** — 現在のスコープで直前に実行されたステップ自身の結果。名前は無く、一時的です。bare な `pipe` として読みます(`ctx.pipe` ではありません — `pipe` は named store ではなく、コンテキストのトップレベルキーです)。`output:` を宣言しないステップも、次のステップのための `pipe` data は生成します。単に永続的な名前が付かないだけです。

つまり `output: X` は同時に 2 つのことを行います: 同じシーケンスの次のステップの `pipe` になる、*かつ* `ctx.X` として永続的に書き込まれ、スコープが終わるまで(下記参照)それ以降のスコープ内のすべてのステップから見えるようになります(直後のステップだけではありません)。

**実例のトレース** — 3 つのステップで、各境界で `ctx` と `pipe` が何を保持しているかを正確に示します:

```yaml
steps:
  - transform: {value: "ctx.name + '!'", output: greeting}   # ステップ 0
  - tool: {name: shout, args: {text: !expr pipe}, output: shouted}  # ステップ 1
  - transform: {value: "ctx.shouted + ' (done)'", output: final}   # ステップ 2
```

`ctx = {name: "Reyn"}` でシードした場合:

| ステップの前 | `ctx` | `pipe` |
|---|---|---|
| 0 | `{name: "Reyn"}` | `null`(先行ステップ無し) |
| 1 | `{name: "Reyn", greeting: "Reyn!"}` | `"Reyn!"`(ステップ 0 の結果) |
| 2 | `{name: "Reyn", greeting: "Reyn!", shouted: "REYN!"}` | `"REYN!"`(ステップ 1 の結果) |
| *(ステップ 2 の後)* | `{..., final: "REYN! (done)"}` | `"REYN! (done)"` |

ステップ 1 はステップ 0 の結果を、両方とも機能する 2 通りの方法で読めます: bare な `pipe`(直前のステップ)か `ctx.greeting`(永続的な store)です — ステップ 0 の直後はどちらも同じ値を保持しますが、間に別のステップが挟まると `ctx.greeting` だけが到達可能なままです。

**スコープの例外**(規範的な記述は下記の[形式文法 § 構造的な不変条件](#formal-grammar)参照):

- `for_each`/`parallel` のブランチはそれぞれ、ブランチ開始時点での外側の `ctx` の**隔離されたコピー**に対して評価します — ブランチ内の書き込みが外側のスコープや sibling ブランチに漏れ出すことはありません。
- `call`/`match` の callee の `ctx` は `pass:` によってバインドされた名前**のみ**から構築されます — バインドされていない名前は、`ctx` のショートカットであっても callee から不可視です。各 `pass:` エントリは明示的な `{NAME: EXPR}` マッピングです: `EXPR` は caller の**現在の完全なコンテキスト**(`ctx`/`pipe`/`item`/`acc` — スコープにあるものすべて、`transform.value` と全く同様)に対して評価される R1 式で、その結果が callee の `ctx` の `NAME` にバインドされます(下記の `for_each`/`fold` の各セクションと[ステップ間のデータフロー](#data-flow-between-steps)の具体例参照)。式の評価に失敗した場合は、そのエントリを名指ししてステップを失敗させます。
- `fold` の `do` と `for_each` の `do` は、それぞれ自身のコンテキストに `ctx`/`pipe` に加えて追加のトップレベルキー(`fold` は `item`/`acc`、`for_each` は `item`)を持ちます — 下記のそれぞれのセクション参照。

**ループ変数をサブパイプラインへ転送する。** `item`/`acc` は named store ではなくトップレベルのコンテキストキーです — そのため `agent` ステップの `{item}` プロンプトはそれらを直接読めますが、`do:` として使われる `call`/`match` ステップも同じ方法で到達できます: その `EXPR` は現在の `do` スコープのコンテキストに対して評価されるので、`item`/`acc` はスコープ内にある単なる名前の一つに過ぎません:

```yaml
pipeline: outer
steps:
  - for_each:
      over: ctx.suspects
      on_error: abort
      do:
        call:
          pipeline: interrogate
          pass:
            suspect: item
      collect: {transform: {value: "pipe"}}
      output: verdicts
```

ここで `interrogate`(登録済みのサブパイプライン)は現在の suspect を `ctx.suspect` として読みます — `pass: {suspect: item}` はベアパス式 `item` を `for_each` スコープのコンテキスト(`ctx`/`pipe` に加えて `item` を持つ)に対して評価し、その結果を callee の `ctx` の `suspect` にバインドしました。`fold` も同様に動作し、`do` スコープにはさらに `acc`(実行中のアキュムレータ)が含まれ、同じ方法で到達可能です: `pass: {running: acc}`。

### `pipeline:` ドキュメントのキー

| キー | 必須 | 意味 |
|-----|------|------|
| `pipeline` | 必須 | 宣言された名前。登録時、および `call`/`match` ステップのターゲットにとって authoritative — [Pipeline registration § 宣言された名前が authoritative](../../concepts/runtime/pipeline-registration.md#the-declared-name-is-authoritative) 参照。 |
| `description` | 任意 | 人間可読な要約。登録済み pipeline が `pipeline__<name>` カタログアクションとして列挙される際、名前と併せて LLM に提示される。デフォルトは空。 |
| `steps` | 必須 | 順に実行される、空でないステップのリスト(下記の「ステップ種別」と「合成プリミティブ」参照)。 |

`input` / `defaults` / `refine` は pipeline 設計のより完全な文法の一部ですが、まだランタイムがありません — これらを使ったドキュメントは、静かに無視されるのではなく、明示的な「まだサポートされていない」エラーで parse に失敗します。

## 形式文法 {#formal-grammar}

以下の EBNF は**規範的な、現行の**文法です — 初期の設計提案からではなく、`parse_pipeline_dsl`(`src/reyn/core/pipeline/parser.py`)から直接導出されています。今日パーサが受け付けるものを正確にカバーしています: これに適合する定義はクリーンに parse でき、違反は拒否されます。YAML のマッピングキーは無順序です — 以下の線形な並びは可読性のためであり、位置的な要求ではありません。`NAME` は識別子的な bare 文字列、`EXPR` は [R1 expression](#r1-expression) のソース文字列、`TPL` は `agent.prompt` のテンプレート文字列(`{ctx.dotted.path}` / `{pipe}` 補間、R1 ではない)です。

```ebnf
Document      ::= YamlDoc ("---" YamlDoc)*        (* テキスト全体で PipelineDoc はちょうど 1 つ *)
YamlDoc       ::= SchemaDoc | PipelineDoc

SchemaDoc     ::= "schema:" NAME "fields:" FieldMap
PipelineDoc   ::= "pipeline:" NAME
                  ("description:" STRING)?
                  "steps:" Step+

Step          ::= "transform:" TransformBody
                 | "tool:"      ToolBody
                 | "shell:"     ShellBody
                 | "agent:"     AgentBody
                 | "call:"      CallBody
                 | "match:"     MatchBody
                 | "fold:"      FoldBody
                 | "for_each:"  ForEachBody
                 | "parallel:"  ParallelBody

TransformBody ::= "{" "value:" EXPR ["output:" NAME] "}"

ToolBody      ::= "{" "name:" STRING
                      ["args:" ArgMap]
                      ["schema:" NAME]
                      ["output:" NAME] "}"
ArgMap        ::= "{" (KEY ":" ArgValue ("," KEY ":" ArgValue)*)? "}"
ArgValue      ::= LITERAL | "!expr" EXPR        (* !expr は値全体としてのみ、ネスト不可 *)

ShellBody     ::= "{" "command:" ArgValue
                      ["schema:" NAME]
                      ["output:" NAME]
                      ["timeout:" INT] "}"

AgentBody     ::= "{" "prompt:" TPL
                      ["identity:" NAME]
                      ["capabilities:" "{" "tools:" "[" NAME* "]" "}"]
                      ["schema:" NAME]
                      ["output:" NAME] "}"

CallBody      ::= "{" "pipeline:" NAME            (* 静的リテラル、EXPR ではない *)
                      ["pass:" "{" (NAME ":" EXPR)* "}"]
                      ["output:" NAME] "}"

MatchBody     ::= "{" "on:" EXPR
                      "cases:" "{" (LABEL ":" MatchTarget)+ "}"
                      ["default:" MatchTarget]
                      ["output:" NAME] "}"
MatchTarget   ::= "{" "pipeline:" NAME ["pass:" "{" (NAME ":" EXPR)* "}"] "}"

FoldBody      ::= "{" [ListSource]
                      "init:" EXPR
                      "do:" Step
                      "output:" NAME              (* 必須。call とは異なる *)
                      ["max_items:" INT] "}"

ForEachBody   ::= "{" [ListSource]
                      ["max_parallel:" INT]
                      "on_error:" OnError          (* 必須 — デフォルト無し *)
                      "do:" Step
                      "collect:" Step
                      ["output:" NAME] "}"

ParallelBody  ::= "{" ["on_error:" OnError]        (* 任意 — デフォルトは "abort" *)
                      "branches:" "{" (NAME ":" Step)+ "}"
                      "collect:" Step
                      ["output:" NAME] "}"

ListSource    ::= "over:" EXPR | "items:" "[" LITERAL* "]"   (* 互いに排他的 *)
OnError       ::= "continue" | "abort" | "retry(" INT ")"

FieldMap      ::= "{" (NAME ":" FieldType)+ "}"
FieldType     ::= "{" "type:" ("bool" | "string" | "number") "}"
                 | "{" "type:" "enum" "values:" "[" LITERAL+ "]" "}"
                 | "{" "type:" "list" "of:" FieldType "}"     (* 'of' は list 不可。リストのリスト無し *)
                 | "{" "type:" "object" "fields:" FieldMap "}"
                 | "{" "type:" "ref" "schema:" NAME "}"
```

文法だけでは示されない構造的な不変条件(単なる慣習ではなく、パーサとエグゼキュータによって強制されます):

- `call`/`match`/`fold` の `do`、`for_each` の `do`/`collect`、`parallel` の branch/`collect` は**完全にネストされた `Step`** です — どのステップ種別でもよく、別の合成プリミティブでも構いません。
- `call`、`match` の case/`default`、`parallel` の `branches` は、いずれも**静的リテラル**の pipeline / ステップターゲットを指定します — 実行時の expression では決してありません。実行時に評価されるのは `match.on` と `for_each`/`fold` の `over` だけです。
- `pass:` は `call`/`match` の callee のコンテキストが構築される*唯一の*チャネルです — 各エントリの `EXPR` が caller のコンテキストに対して評価され、そのエントリの `NAME` にバインドされます。どのエントリにもバインドされていない `NAME` は callee から不可視です([ステップ間のデータフロー](#data-flow-between-steps)参照)。
- `for_each`/`parallel` のブランチはそれぞれ、外側の named store の**隔離されたコピー**を受け取ります — 並行なアイテム/ブランチ間の sibling 通信はありません([ステップ間のデータフロー](#data-flow-between-steps)参照)。
- `!expr` は `tool`/`shell` の引数を解決すべき expression にする*唯一の*方法です — リスト / マッピングの中にネストすると静かな no-op ではなく parse エラーになります。

## ステップ種別

すべてのステップは、自身の種別を名前とする単一キーのマッピングです。3 つは**線形な leaf ステップ**です — コンテキストを読み、ひとつの作業を行い、結果を生成します:

### `transform`

純粋なステップです: `value` は現在のコンテキスト(`ctx`/`pipe` — [ステップ間のデータフロー](#data-flow-between-steps)参照)に対して [R1 expression](#r1-expression) として評価され、その結果がこのステップの pipe data になります(`output` が設定されていれば、その named store にも書き込まれます)。

```yaml
- transform: {value: "'Hello, ' + ctx.name + '!'", output: greeting}
```

| キー | 必須 | 意味 |
|-----|------|------|
| `value` | 必須 | R1 expression のソース。 |
| `output` | 任意 | 結果を書き込む named store。 |

### `tool`(+ `shell` シュガー)

副作用を伴うステップです: `name` を `args` と共に、ライブな `invoke_action` 呼び出しと同じ「qualified action ルーティング → bare lookup」の順で dispatch します — そのため `tool` ステップは qualified action(`file__read`)にも bare な登録済みツール名(`web_search`)にも名前を付けられます。

```yaml
- tool: {name: web_search, args: {query: !expr ctx.brief, limit: 5}, output: results}
```

| キー | 必須 | 意味 |
|-----|------|------|
| `name` | 必須 | ツール / アクション名(リテラル文字列)。 |
| `args` | 任意 | 引数名 → 値 のマッピング。各値は `!expr` タグが付いていない限り**リテラル**([リテラル vs `!expr`](#vs-expr)参照)。 |
| `schema` | 任意 | 結果が適合すべき登録済み schema 名(`verify: schema` — [Schema](#schema-verify-schema)参照)。不適合はステップを失敗させる。 |
| `output` | 任意 | 結果を書き込む named store。 |

`shell` は `"shell"` という名前の `tool` ステップのシュガーです: `command` は operator のサンドボックス(`sandboxed_exec` — 直接の `sandboxed_exec` 呼び出しと同じ閉じ込め、ポリシー優先順位、監査イベント)の中で `/bin/sh -c <command>` として実行されます。ステップに入ってくる pipe data はプロセスの **STDIN に JSON エンコードされて**渡されます。プロセスの **STDOUT がステップの結果になります** — JSON として parse できればデコードされ(`dict` を要求する `verify: schema` が JSON を出力するコマンドに適用できるように)、できなければ生のテキストのままです。

```yaml
- shell: {command: !expr "'ls ' + ctx.dir", output: listing}
```

| キー | 必須 | 意味 |
|-----|------|------|
| `command` | 必須 | リテラルまたは `!expr`。`tool` ステップの `args` の値と同じルール。`/bin/sh -c <command>` として実行される。 |
| `schema` | 任意 | `tool` と同じ。 |
| `output` | 任意 | `tool` と同じ。 |
| `timeout` | 任意 | 秒単位のウォールクロック時間制限。デフォルトは 60。 |

`shell` ステップの起動は、`exec__sandboxed_exec` と同じ `HIGH` severity の capability floor 上にあります — untrusted-content floor、または unbound な delegate の floor に narrowing されたコンテキストは、`sandboxed_exec` を直接呼び出せないのと同様に、`shell` ステップも実行できません。

#### リテラル vs `!expr`

`tool`/`shell` の引数値は — YAML タグ `!expr` が付いていない限り — **リテラル**です。書かれた通りそのままツールに渡されます:

```yaml
args: {query: !expr ctx.brief, limit: !expr "ctx.n + 1", label: "a plain string"}
```

`query` と `limit` は R1 expression のソースで、実行時にステップの `ctx`/`pipe` コンテキストに対して解決されます([ステップ間のデータフロー](#data-flow-between-steps)参照)。`label` はリテラル文字列 `"a plain string"` です。`!expr` は引数の**値全体**としてのみ有効です — ネストしたリスト / マッピングの中に隠れていると parse エラーになるため、「たまたま expression に見えるリテラル」と「expression」の間に曖昧さはありません。

`transform.value` は常に R1 expression です(`transform` ステップにリテラル形式は無いため `!expr` タグは不要)。`agent` ステップの `prompt` は決して R1 expression ではありません — 下記参照。

### `agent`

LLM 駆動の leaf ステップです: `prompt`(テンプレート文字列)が現在のコンテキストに対して補間され、`identity`(省略時は起動者自身)の下で `capabilities`(省略時は起動者自身のプロファイル)に capability を narrowing された ephemeral session の中で 1 ターンとして実行されます。

```yaml
- agent: {prompt: "Summarize: {ctx.doc}", capabilities: {tools: [file__read]}, schema: Summary, output: summary}
```

| キー | 必須 | 意味 |
|-----|------|------|
| `prompt` | 必須 | テンプレート文字列 — `{ctx.dotted.path}` / `{pipe}` 参照が補間される(値のみ、演算子なし — これは R1 expression ではなく文字列補間)。 |
| `identity` | 任意 | 実行するエージェント identity。デフォルトは run の起動者。**登録済み** pipeline は任意の identity を指定できるが、**inline で エージェントが生成した** pipeline は起動者自身の identity のみ指定可能 — 別のエージェントの identity を指定すると capability escalation として静的解析ゲートに拒否される([Ad-hoc inline 起動](#ad-hoc-inline)参照)。 |
| `capabilities` | 任意 | `{tools: [NAME*]}` — ephemeral session のツール surface を narrowing する。restrict-only: pipeline のステップが起動者自身の envelope を超えることは決してない。 |
| `schema` | 任意 | `tool` と同じ `verify: schema` セマンティクスを、parse された JSON 応答に適用。 |
| `output` | 任意 | 結果を書き込む named store。 |

どこで到達しても(トップレベルでも `for_each` 経由の fan-out でも)、すべての `agent` ステップは run 共有の spawn budget を消費します — [Safety caps](#safety-caps)参照。

## 合成プリミティブ

5 つのプリミティブがステップを非線形な制御フローに合成します — Appendix-B の完全な集合で、今日すべてサポートされています。

### `call` — sub-pipeline

**登録済み**の sub-pipeline を静的な名前で同期実行し、その最終出力をこのステップの結果として通します。

```yaml
- call:
    pipeline: validate_doc
    pass:
      doc: ctx.doc
      rules: ctx.rules
    output: validation
```

| キー | 必須 | 意味 |
|-----|------|------|
| `pipeline` | 必須 | 静的なリテラルの pipeline 名 — 実行時の expression ではない。未登録のターゲットはステップを失敗させる。 |
| `pass` | 任意 | フラットな `{NAME: EXPR}` マッピング。callee のコンテキストはこれらのバインディングのみから**新規に**構築される — どのエントリにもバインドされていない `NAME` は callee から構造的に不可視。各エントリの `EXPR` は caller の現在のコンテキスト(`ctx`/`pipe`/`item`/`acc` — スコープにあるものすべて、`transform.value` と全く同様)に対して評価される R1 expression で、その結果が callee の `ctx` の `NAME` にバインドされる([ステップ間のデータフロー](#data-flow-between-steps)参照)。式の評価に失敗するとそのエントリを名指ししてステップが失敗する。 |
| `output` | 任意 | callee の最終結果を書き込む named store。 |

callee の最初のステップは call サイトでの caller の pipe data を受け取る。callee 自身の最終ステップの出力がこの `call` ステップの結果になる。callee の失敗は `call` ステップを失敗させる。

### `match` — 実行時選択の sub-pipeline

`on` を値へと評価し、その値と文字列として等しいラベルを持つ case を選択して、`call` ステップと全く同様にそのターゲットを実行します。

```yaml
- match:
    on: "ctx.review.passed"
    cases:
      "True": {pipeline: report_pass, pass: {review: ctx.review}}
      "False": {pipeline: report_fail, pass: {review: ctx.review}}
    default: {pipeline: report_unknown}
    output: report
```

| キー | 必須 | 意味 |
|-----|------|------|
| `on` | 必須 | 現在のコンテキストに対して評価される R1 expression。その文字列化された結果が case ラベルを選択する。 |
| `cases` | 必須 | `LABEL: {pipeline, pass?}` の空でないマッピング — 各ターゲットは `call` と全く同様の静的リテラル名(`pass:` のフラットな NAME -> R1-EXPRESSION マッピングも `call` と同じ)。 |
| `default` | 任意 | どの case ラベルにもマッチしなかった場合に実行される `{pipeline, pass?}`。マッチする case が無く `default` も無いステップは失敗する。 |
| `output` | 任意 | 選択された callee の結果を書き込む named store。 |

すべての case / `default` のターゲットは静的リテラルです — 実行時の値が選ぶのは常に*ラベル*であり、ターゲット自体ではありません。

### `fold` — 逐次アキュムレータ

リストを順に走査し、繰り返される `do` ステップを通してアキュムレータを引き継ぎます。`do` のコンテキストは、通常の `ctx`/`pipe`([ステップ間のデータフロー](#data-flow-between-steps)参照)に加えて `item` と `acc` という 2 つの追加のトップレベルキーを持ちます — 詳細は下の表を参照。

```yaml
- fold:
    over: ctx.items
    init: "0"
    do: {transform: {value: "acc + item"}}
    output: total
    max_items: 1000
```

| キー | 必須 | 意味 |
|-----|------|------|
| `init` | 必須 | 最初のイテレーションの前に一度だけ評価され、`acc` の初期値となる R1 expression。 |
| `do` | 必須 | リストの各アイテムごとに 1 回、`{ctx, pipe, item, acc}` というコンテキストで再実行される単一のステップ — `item` は現在の要素、`acc` は実行中のアキュムレータ。`do` の戻り値が次の `acc` になる。 |
| `output` | 必須 | 最終的な `acc` を書き込む named store(`fold` の存在意義は名前付き結果を生成すること — `call` の任意な `output` とは異なり必須)。 |
| `over` | 任意* | 走査するリストへと解決される R1 expression。 |
| `items` | 任意* | 静的なリテラルリスト。 |
| `max_items` | 任意 | 走査を先頭 N 要素に制限する(それより長いソースは静かに切り詰められ、エラーにはならない)。 |

\* `over` と `items` は互いに排他的です。どちらも無ければ、リストはステップの受け取った pipe data にフォールバックします。アイテムの失敗は fold 全体を失敗させます。(`for_each` と異なり)`collect` はありません — 各アイテムの結果はそれより前のアイテムで積み上がった状態に依存するため、独立して collect するものが無いのです。

`item`/`acc` は `do` 自身のステップを超えても到達可能です — `do: {call: {pipeline: X, pass: {current: item}}}`(または `pass: {running: acc}`)は、現在の要素(または実行中のアキュムレータ)を `call`/`match` サブパイプラインへ転送します。これは `agent` の `do` の `{item}`/`{acc}` プロンプト参照がすでに到達できていたのと同じ変数です。

### `for_each` — 並行 fan-out

各リストアイテムに対して `do` を隔離された並行サブスコープとして実行し、その後 `collect` を一度だけ結果に対して実行します。このセクションの `do` コンテキストが依拠する隔離ルール(各アイテムが自身の `ctx` コピーを持ち、書き込みがアイテム間や外側のスコープに漏れ出すことはない)については[ステップ間のデータフロー](#data-flow-between-steps)参照。

```yaml
- for_each:
    over: ctx.reviewers
    max_parallel: 4
    on_error: "retry(2)"
    do: {agent: {prompt: "Review as {item}: {ctx.doc}", schema: Review}}
    collect: {transform: {value: "pipe"}}
    output: reviews
```

| キー | 必須 | 意味 |
|-----|------|------|
| `do` | 必須 | アイテムごとに一度、`{ctx, pipe, item}` というコンテキストで実行されるステップ — `ctx` は外側の named store の隔離された**コピー**(アイテム間の sibling 可視性なし)、`pipe` はこのステップ自身が受け取った pipe data で全アイテムを通じて一定。 |
| `collect` | 必須 | fan-out の後に一度だけ、生き残ったアイテム結果の順序付きリストに対して実行されるステップ(その `pipe` コンテキスト)。その結果がこのステップ全体の結果になる。 |
| `on_error` | 必須 | `continue`(失敗したアイテムは結果から除外され、resume で再実行されることは無い)、`abort`(失敗したアイテムは残りの pending アイテムをキャンセルしステップ全体を失敗させる)、`retry(N)`(失敗したアイテムの `do` を最大 N 回追加で再実行し、それでも失敗すれば `abort` にフォールバック)のいずれか。 |
| `over` | 任意* | `fold` と同じ。 |
| `items` | 任意* | `fold` と同じ。 |
| `max_parallel` | 任意 | ライブな並行度の上限(`Semaphore`)。省略時は控えめな有限値がデフォルト — 省略による無制限にはならない。 |
| `output` | 任意 | `collect` の結果を書き込む named store。 |

\* `over`/`items` は `fold` と同様に互いに排他的で、無ければ受け取った pipe data にフォールバックします。(`fold` 専用の)アイテムレベルの `acc` はありません — あるアイテムが他のアイテムの結果を見ることはできません。

`item` は `fold` の `item`/`acc` と同様に `do` 自身のステップを超えても到達可能です — `do: {call: {pipeline: X, pass: {current: item}}}` は、現在の要素を `do:` として使われる `call`/`match` サブパイプラインへ転送します。

### `parallel` — 異種・名前付きブランチの fan-out

`for_each` の異種版の兄弟です: 実行時サイズのリストに対して単一の `do` を fan-out する代わりに、`parallel` は静的で有限な、*それぞれ異なる*名前付きブランチの集合を並行に fan-out し、その後 `collect` を一度だけそれらの結果の名前付きマップに対して実行します。`for_each` と同じ隔離ルールです — [ステップ間のデータフロー](#data-flow-between-steps)参照: 各ブランチは自身の `ctx` コピーを持ち、`collect` の `pipe` はどれか 1 つのブランチの結果ではなく `{branch_name: result}` マップ全体です。

```yaml
- parallel:
    on_error: "abort"
    branches:
      security: {agent: {prompt: "Security-review {ctx.doc}", schema: Review}}
      style: {agent: {prompt: "Style-review {ctx.doc}", schema: Review}}
    collect: {transform: {value: "{security: pipe.security, style: pipe.style}"}}
    output: reviews
```

| キー | 必須 | 意味 |
|-----|------|------|
| `branches` | 必須 | 空でない `{NAME: Step}` マッピング — 各ブランチはそれぞれ独立した形のステップです(名前ごとに種別 / 設定が異なってよい)。`for_each` の、アイテムごとに再実行される単一の `do` とは異なります。すべてのブランチが並行に実行され、ブランチ数そのものが並行度の上限です(`max_parallel` は無い — 集合は静的に有限であるため)。 |
| `collect` | 必須 | すべてのブランチが着地した後に一度だけ、**名前付きマップ** `{branch_name: result}` に対して実行されるステップ(`for_each` と異なり順序付きリストではない)。その結果がこのステップ全体の結果になる。 |
| `on_error` | 任意 | `continue`、`abort`(省略時のデフォルト — `on_error` が必須の `for_each` とは異なる)、または `retry(N)` のいずれか — `for_each` の `on_error` と同じセマンティクス。`continue` でドロップされたブランチのキーは `collect` の名前付きマップから欠落します。 |
| `output` | 任意 | `collect` の結果を書き込む named store。 |

各ブランチのコンテキストは `{ctx, pipe}` です — `ctx` は外側の named store の隔離されたコピー、`pipe` はこのステップ自身が受け取った pipe data で全ブランチを通じて一定です。(`for_each`/`fold` 専用の)`item`/`acc` は無く、ブランチ間の sibling 可視性もありません。

## R1 expression 言語

`transform.value`、`!expr` タグの付いた `tool`/`shell` の引数、`match.on` は、いずれも同じ小さな**total**な expression 言語(R1)に対して解決されます — 汎用のスクリプト言語でも、コード実行サンドボックスでもない、専用のツリーウォーク・インタプリタです。再帰も、ユーザー定義関数も、無限ループも(すべてのコンビネータは既に実体化された 1 つのリストをちょうど 1 回だけ走査する)、IO も、`eval`/`exec` もありません。

```ebnf
expr           ::= or_expr
or_expr        ::= and_expr ("or" and_expr)*
and_expr       ::= not_expr ("and" not_expr)*
not_expr       ::= "not" not_expr | comparison
comparison     ::= additive (cmp_op additive)?
additive       ::= multiplicative (("+" | "-") multiplicative)*
multiplicative ::= unary (("*" | "/") unary)*
unary          ::= "-" unary | primary
primary        ::= NUMBER | STRING | "true" | "false" | "null"
                  | "(" expr ")"
                  | "[" (expr ("," expr)*)? "]"
                  | "{" (IDENT ":" expr ("," IDENT ":" expr)*)? "}"
                  | combinator
                  | path
combinator     ::= "map" "(" expr "," lambda ")"
                  | "filter" "(" expr "," lambda ")"
                  | "all" "(" expr "," lambda ")"
                  | "any" "(" expr "," lambda ")"
                  | "find" "(" expr "," lambda ")"
                  | "count" "(" expr ")"
                  | "sum" "(" expr ")"
                  | "join" "(" expr "," expr ")"
                  | "get" "(" expr "," STRING ("," expr)? ")"
                  | "parse_json" "(" expr ")"
lambda         ::= IDENT "->" expr        (* コンビネータ自身の引数としてのみ有効 *)
path           ::= IDENT ("." IDENT)*
cmp_op         ::= "==" | "!=" | "<" | ">" | "<=" | ">="
```

**リテラル**: `true` / `false` / `null`、整数、浮動小数点数、シングルまたはダブルクォート文字列。

**フィールド参照**: コンテキストに対するドット区切りのパス。例: `ctx.review.passed`、または bare な `pipe`。パスが存在しないか、中間セグメントが非マッピングであれば例外を送出します — bare なパスは安全なナビゲーションではありません。それには下記の `get(...)` を使ってください。

**演算子**: `and` / `or` / `not`。比較 `==` `!=` `<` `>` `<=` `>=`(`<`/`>`/`<=`/`>=` は 2 つの数値または 2 つの文字列を要求、`==`/`!=` は何にでも使える)。算術 `+` `-` `*` `/`(数値。`+` は文字列とリストの連結にも使える)。ゼロ除算は例外を送出します。

**コンビネータ** — この文法が持つ唯一の呼び出しに似た構文で、固定された閉じた集合です:

| コンビネータ | シグネチャ | 意味 |
|---|---|---|
| `map` | `map(list, item -> expr)` | 各要素を変換。 |
| `filter` | `filter(list, item -> expr)` | ラムダが true となる要素を残す。 |
| `all` | `all(list, item -> expr)` | 全要素がラムダを満たせば true。 |
| `any` | `any(list, item -> expr)` | いずれかの要素がラムダを満たせば true。 |
| `find` | `find(list, item -> expr)` | 最初にマッチした要素、無ければ `null`。 |
| `count` | `count(list)` | 要素数。 |
| `sum` | `sum(list)` | 数値の合計。 |
| `join` | `join(list, sep)` | 文字列 join。 |
| `get` | `get(base, "dotted.path", default?)` | **安全な**ナビゲーション — bare な `Path` と異なり、パスが存在しなくても例外を送出せず、`default`(無ければ `null`)を返す。 |
| `parse_json` | `parse_json(string)` | JSON 文字列をその値(object/array/string/number/bool/null)にデコードする。引数が文字列でないか、有効な JSON でなければ例外を送出する — 安全なデフォルト返却バリアントは存在しない。 |

`lambda`(`item -> expr`)は `map`/`filter`/`all`/`any`/`find` の直接の引数としてのみ有効です — 代入したり受け渡したりできる値ではありません。この固定コンビネータ集合の外にある名前を関数呼び出しとして書くと parse エラーになります。

expression の例: `"'Hello, ' + ctx.name + '!'"`、`"ctx.n + 1"`、`"all(ctx.reviews, r -> r.passed)"`。

`agent` ステップの `prompt` は**別の**仕組みです: `{ctx.dotted.path}` / `{pipe}` 参照が単なる値として補間されるテンプレート文字列であり — R1 expression ではなく、波括弧の中に演算子はありません。

## Schema — `verify: schema`

schema はネストされた単相(ジェネリクスなし)型に名前を付けたものです: 各フィールドはスカラー(`bool`/`string`/`number`)、`enum`、型付きの `list`(要素型 `of` は必須 — 型なしリストは無く、リストのリストも許可されない)、ネストされたインラインの `object`、または別の登録済み schema への `ref`(登録済み集合全体にわたる再帰参照のサイクルは登録時に拒否される)のいずれかです。

```yaml
schema: Review
fields:
  passed: {type: bool}
  notes: {type: string}
  tags: {type: list, of: {type: string}}
```

`tool`/`shell`/`agent` ステップの `schema: NAME` キーは、その結果(`agent` の場合は parse された JSON 応答)が適合すべき登録済み schema を指定します — 不適合はステップを失敗させます。同じ DSL ドキュメント集合内で宣言された schema(独立した `schema:` ドキュメント)は、[ad-hoc な inline pipeline](#ad-hoc-inline) でもこれを可能にするものです。その schema は同じ定義文字列と共に移動するためです。

## 起動

pipeline を起動するツールは 4 つあります。いずれも同じ実行に収束します: 起動は専用の `PipelineExecutorDriver` セッションを spawn し、pipeline はその中で走ります([Driver-as-session](../../concepts/runtime/pipelines.ja.md#driver-as-session)参照)— これら 4 つのどれも、呼び出し元自身のターン上でインラインに pipeline を実行しません。

| ツール | 登録済み / inline | 同期 / 非同期 |
|------|---------------------|---------------|
| `run_pipeline` | 登録済み、`name` 指定 | 同期 — attached、terminal まで block |
| `run_pipeline_async` | 登録済み、`name` 指定 | 非同期 — detached、即座に返る |
| `run_pipeline_inline` | inline、ad-hoc な `definition` 文字列 | 同期 — attached、terminal まで block |
| `run_pipeline_inline_async` | inline、ad-hoc な `definition` 文字列 | 非同期 — detached、即座に返る |

### 登録済み起動

`run_pipeline(name, input?)` と `run_pipeline_async(name, input?)` は登録された名前で pipeline を検索します([Pipeline registration](../../concepts/runtime/pipeline-registration.md)参照)。`input` は pipeline の最初のステップの初期 named context(`ctx.*`)をシードします — シード入力を必要としない pipeline では省略できます。登録されていない `name` は明確に失敗します。

### 同期 vs 非同期

- **同期**(`run_pipeline`、`run_pipeline_inline`): 呼び出し元は driver-session の run に attach し、terminal 状態に達するまで block して、結果を in-band で読み戻します(`{status: "ok", data: {run_id, output, named_stores}}`、または `error`/`cancelled`)。ライブな `pipeline_step_started` / `pipeline_step_completed` イベントが run の間、呼び出し元にストリームされ(TUI のライブビューが描画するもの)、協調的な Ctrl-C は次のステップ境界で run をクリーンに停止させます。attach 自体がクラッシュで中断された場合、run は失われません — 非同期と同じ recovery パスに引き渡され、結果は代わりに後で inbox メッセージとして届きます(`{status: "started", data: {run_id}}`)。
- **非同期**(`run_pipeline_async`、`run_pipeline_inline_async`): 即座に `{status: "started", data: {run_id}}` を返します。最終結果は後で `[pipeline]` inbox メッセージとして届きます。

### Ad-hoc inline 起動

`run_pipeline_inline(definition, input?)` と `run_pipeline_inline_async(definition, input?)` は、呼び出しているエージェントが実行時に生成する pipeline DSL 文字列を受け取ります — 登録済み pipeline ファイルと同じ Appendix-B 文法で、その定義自身のステップが参照する `schema:` ドキュメントも含みます。事前登録は不要です: 文字列は parse され、何かが spawn される前に**静的解析ゲート**を通されます。そのため不正な定義は明確に失敗し、何も spawn しません:

1. 定義が parse できる。
2. すべてのステップの `schema:` 参照が、定義自身の schema 内で解決する。
3. すべての `tool` ステップ名が、登録済みツールまたは qualified action に解決する。
4. *(構造的、実行時チェックではない)* driver-session は起動者自身の identity の下で spawn され、restrict-only で narrowing されるため、生成された pipeline が起動者自身の envelope を超えることは構造上あり得ない。
5. どの `tool` ステップも pipeline を起動したり delegate したりしない — ネストは `call` のみ。
6. **Inline 専用**: `agent` ステップの `identity` は、設定されている場合、起動者自身の identity と等しくなければならない。登録済み pipeline はこのチェックの対象外(信頼された登録者が意図的に identity を選んだため)。別の identity を指定する inline の、エージェントが生成した pipeline は capability escalation として拒否される。

inline run は、登録済みのものと全く同様に crash-recoverable です — その完全な parse 済み定義(schema を含む)が work-order に永続化されるため、recovery は再 parse も再検索も必要としません。

## Safety caps

`reyn.yaml` の `safety.spawn` ブロック内の 2 つの operator 設定キャップが、pipeline run の fan-out に境界を課し、すべての `run`/`resume` 呼び出しに渡されます:

```yaml
# reyn.yaml
safety:
  spawn:
    max_pipeline_fan_out_depth: 5   # デフォルト
    max_pipeline_spawns: 100        # デフォルト
```

| キー | デフォルト | 意味 |
|-----|-----------|------|
| `max_pipeline_fan_out_depth` | `5` | `for_each` fan-out スコープの最大**ネスト深度**(トップレベルの `for_each` は深度 1、別の `for_each` の `do`/`collect` の中の `for_each` は深度 2、…)。これを超える `for_each` は spawn せずステップを失敗させる。`0` = 無制限。 |
| `max_pipeline_spawns` | `100` | **1 つの pipeline run** がそのすべての `agent` ステップ(トップレベルでも `for_each` 経由でも)を通じて spawn できる ephemeral session の最大数。run ごとの単調カウンタ。キャップを超える spawn はステップを失敗させる。`0` = 無制限。 |

いずれも控えめな有限値がデフォルトです — run は省略によって無制限にはなりません。どちらのキャップも LLM が実行時に到達できるものではなく、両方とも operator 設定かつ再起動時のみ反映されます。

## セキュリティ

[Pipeline registration § セキュリティ](../../concepts/runtime/pipeline-registration.md#security-launching-a-pipeline-stays-gated)参照: pipeline を起動すること(上記 4 ツールのいずれでも)は、別のエージェントへの delegate と同じ `HIGH` severity の、spawn-adjacent な capability floor に位置します。untrusted-content floor、または unbound な delegate の floor に narrowing されたコンテキストは、登録済みであれ inline であれ、pipeline を起動できません。

## 文法(生成用)

実行時に pipeline 定義を作成するエージェント(例: `run_pipeline_inline`)向けの、コンパクトで自己完結したブロックです — 文法そのものに加え、文法だけからは導けないルール、そして 1 つの規範的な例。このセクションは単独で成立します。上記の文章を読んでいることを前提としません。

**文法** — 上記の形式文法と同じ EBNF を、参照の便宜のため再掲します:

```ebnf
Document      ::= YamlDoc ("---" YamlDoc)*        (* 全体でちょうど 1 つの PipelineDoc *)
YamlDoc       ::= SchemaDoc | PipelineDoc
SchemaDoc     ::= "schema:" NAME "fields:" FieldMap
PipelineDoc   ::= "pipeline:" NAME ("description:" STRING)? "steps:" Step+

Step          ::= "transform:" "{" "value:" EXPR ["output:" NAME] "}"
                 | "tool:"     "{" "name:" STRING ["args:" ArgMap] ["schema:" NAME] ["output:" NAME] "}"
                 | "shell:"    "{" "command:" ArgValue ["schema:" NAME] ["output:" NAME] ["timeout:" INT] "}"
                 | "agent:"    "{" "prompt:" TPL ["identity:" NAME]
                                    ["capabilities:" "{" "tools:" "[" NAME* "]" "}"]
                                    ["schema:" NAME] ["output:" NAME] "}"
                 | "call:"     "{" "pipeline:" NAME ["pass:" "{" (NAME ":" EXPR)* "}"] ["output:" NAME] "}"
                 | "match:"    "{" "on:" EXPR "cases:" "{" (LABEL ":" MatchTarget)+ "}"
                                    ["default:" MatchTarget] ["output:" NAME] "}"
                 | "fold:"     "{" [ListSource] "init:" EXPR "do:" Step "output:" NAME
                                    ["max_items:" INT] "}"
                 | "for_each:" "{" [ListSource] ["max_parallel:" INT] "on_error:" OnError
                                    "do:" Step "collect:" Step ["output:" NAME] "}"
                 | "parallel:" "{" ["on_error:" OnError] "branches:" "{" (NAME ":" Step)+ "}"
                                    "collect:" Step ["output:" NAME] "}"

MatchTarget   ::= "{" "pipeline:" NAME ["pass:" "{" (NAME ":" EXPR)* "}"] "}"
ArgMap        ::= "{" (KEY ":" ArgValue ("," KEY ":" ArgValue)*)? "}"
ArgValue      ::= LITERAL | "!expr" EXPR
ListSource    ::= "over:" EXPR | "items:" "[" LITERAL* "]"
OnError       ::= "continue" | "abort" | "retry(" INT ")"
FieldMap      ::= "{" (NAME ":" FieldType)+ "}"
FieldType     ::= "{type: bool}" | "{type: string}" | "{type: number}"
                 | "{type: enum, values: [" LITERAL+ "]}"
                 | "{type: list, of:" FieldType "}"
                 | "{type: object, fields:" FieldMap "}"
                 | "{type: ref, schema:" NAME "}"
EXPR          ::= (* 上記の R1 expression 言語を参照 *)
TPL           ::= (* {ctx.dotted.path} / {pipe} 補間を持つ文字列、値のみ *)
```

**Hard rules**(これらのいずれかへの違反は parse エラーか実行時のステップ失敗になります — 静かな誤った結果になることは決してありません):

1. `call` の `pipeline:`、`match` の case/`default` の `pipeline:`、そして各 `parallel` ブランチのステップ: `call`/`match` における `pipeline:` ターゲットは常に静的リテラル名です — 実行時の expression では決してありません。実行時に評価される `match.on` が選ぶのは常に case の*ラベル*であり、ターゲット自体ではありません。
2. `!expr` は `tool`/`shell` の引数を R1 expression としてマークします。それ以外はすべてリテラルで、そのまま渡されます。補間を期待してマークしていない引数の中に `{ctx.x}` と書かないでください — それが機能するのは `agent.prompt`(`TPL`)だけで、そこだけです。
3. `pass:` は、`call`/`match` の callee が caller のスコープの何を見られるかを決める唯一の方法です — 各エントリは明示的な `{NAME: EXPR}` マッピングです(bare-NAME の省略形はありません)。`EXPR` は caller の現在の完全なコンテキスト(`ctx`/`pipe`/`item`/`acc` — スコープにあるものすべて)に対して評価され、その結果が callee の `ctx` の `NAME` にバインドされます。どのエントリにも対応しない `NAME` は callee から不可視であり、暗黙に継承されることはありません — [ステップ間のデータフロー](#data-flow-between-steps)参照。
4. `for_each.on_error` は**必須**です — `continue`/`abort`/`retry(n)` を明示的に指定してください。`parallel.on_error` は任意で、デフォルトは `abort` です。
5. `for_each`/`parallel` のアイテム / ブランチは、他のどのアイテム / ブランチの結果も見ることができません — マージされた集合を見るのは `collect` だけです(`for_each` は順序付きリスト、`parallel` は `{branch_name: result}` マップ)。
6. `fold.output` は必須です(fold の存在意義は名前付きの累積結果を生成すること)。他のすべてのステップ種別の `output` は任意です。
7. すべての `agent` ステップは、起動しているセッション自身の envelope 以下に capability を narrowing されます — 起動者が持つより広い `capabilities` 集合を指定しても、それが付与されることはありません。inline(エージェントが生成した、ファイル登録されていない)定義では、`agent` ステップの `identity` は省略するか起動者自身と等しくなければなりません — それ以外の identity を指定すると拒否されます。
8. `!expr` は `args`/`command` エントリの*値全体*としてのみ有効です — リストやマッピングの値の中にネストすることはできません。

**1 つの規範的な例**(3 つのステップ種別すべて、1 つのプリミティブ、1 つの schema):

```yaml
schema: Review
fields:
  passed: {type: bool}
  notes: {type: string}
---
pipeline: review_and_report
description: Review a document and summarize the verdict.
steps:
  - agent:
      prompt: "Review {ctx.doc}. Reply with passed (bool) and notes (string)."
      schema: Review
      output: review
  - transform:
      value: "ctx.review.passed and 'OK' or 'NEEDS WORK'"
      output: verdict
  - shell:
      command: !expr "'echo ' + ctx.verdict"
      output: shouted
```
