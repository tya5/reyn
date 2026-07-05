---
type: concept
topic: runtime
audience: [human, agent]
search_hints: [pipeline, pipeline DSL, control plane, execution plane, driver-as-session, PipelineExecutorDriver, crash recovery pipeline, run_pipeline, safety by structure, Turing-incomplete]
---

# Pipeline

**pipeline** とは、小さな YAML DSL で書かれた決定的なマルチステップ制御フローです: `transform` / `tool` / `agent` ステップの固定シーケンスに、必要に応じて少数の構造的プリミティブ(`call` / `match` / `fold` / `for_each`)を組み合わせます。エージェントは他のツールと同じように名前 + input でパイプラインを起動し、パイプラインは(起動したエージェントがその後何をするかとは独立に)自身のクラッシュ耐性を持った実行の下で完了(または失敗)まで走ります。

pipeline が存在する理由は、すべてのマルチステップタスクを毎回 LLM がターンごとに再導出すべきではないから、です。繰り返し発生する既知の手順 — N 人のレビュアーにレビューを fan out して結果をマージする、リストを走査して同じ transform を適用する、flaky なチェックを retry してから escalate する — は、エージェントが毎回ゼロから再計画するより、*書かれた*制御フローとして表現する方が信頼性が高く、安価で、監査しやすいものです。pipeline はその書かれた制御フローです: ステップとその合成は DSL の中で固定されており、実行時に変わるのはそこを流れる *データ* だけです。

## Control plane と Execution plane

Reyn には既に非決定的な execution plane が存在します: エージェントターン — LLM が利用可能なツールとコンテキストから次の行動を決める場所です。pipeline はそれと並ぶ**別個の、決定的な control plane** です:

- **Execution plane**(エージェントターン)は判断が宿る場所です — LLM がコンテキストを読み、行動を選びます。その制御フローは事前に固定されておらず、モデルの決定から動的に生じます。
- **Control plane**(pipeline)は既知の形を持つ手順が宿る場所です — そのステップと合成(sequence / branch / fan-out / accumulate)は DSL 内で固定されています。`agent` ステップは両者の継ぎ目です: pipeline のステップは一つの限定された判断を LLM に委譲できます(capability を narrowing された leaf worker として)が、pipeline 自体が自らの形を即興で変えることはありません。

この分離こそが、pipeline を設計上 **Turing-incomplete** にしているものです: プリミティブは合成可能です(`call` は別の pipeline を呼び出せ、`fold` は per-item ステップとして `call` を実行できます)が、一般的な再帰も、動的なステップ生成も、実行中の pipeline が自身のステップリストを書き換えるプリミティブもありません。pipeline の完全なステップグラフはその DSL ドキュメントを読めば分かります — エージェントターンが未想定のツールを呼ぶことを決められるのとは異なり、pipeline は実行時に新しい制御フローを構築できません。

## 構造による安全性(Safety by structure)

制御フローが固定され閉じているため、いくつかの安全性の性質は、上に乗せた実行時ポリシーではなく DSL の形そのものから導かれます:

- **ネストされた起動は不可。** pipeline の `tool` ステップは自分自身で別の pipeline を起動したり別のエージェントに delegate したりできません — ネストは `call` のみです。これにより、エージェントが pipeline を起動する際に許可する cost-bound の承認は、実行中のステップが拡張し得るオープンエンドなものではなく、*既知の*ステップグラフに対する推移閉包のままになります。
- **capability の narrowing は実行時チェックではなく構造的です。** `agent` ステップの ephemeral session は*起動者自身の identity* の下で spawn され、restrict-only で narrowing されます — pipeline のステップは、構造上、それを起動したエージェントの capability envelope を超えることが決してできません。エージェントがその場で生成する ad-hoc な pipeline については([Invocation](pipeline-registration.md) 参照)、別のエージェントの identity を指定するステップは capability escalation となるため、何かが spawn される前に静的ゲートがそれを拒否します。
- **fan-out は「省略時に無制限」ではなく、境界を持ちます。** `for_each` の並行分岐は operator が設定した spawn budget で上限が課されます([Pipeline DSL リファレンス § Safety caps](../../reference/runtime/pipeline-dsl.md) 参照)。これは実行中どこで到達しても — トップレベルでも fan-out 経由でも — `agent` ステップごとに課金されます。これらのステップは通常の spawn-lineage の記帳の外側で ephemeral session を spawn するためです。

## Driver-as-session

pipeline は起動元エージェント自身のターン上でインラインには実行されません。`run_pipeline` / `run_pipeline_async` / `run_pipeline_inline` / `run_pipeline_inline_async` のいずれで起動しても、`PipelineExecutorDriver` を実行する専用セッションが spawn され、pipeline は*その*セッションの中で実行されます。

これは、専用の実行パスではなく通常のセッション基盤を意図的に再利用したものです: driver-session の run-loop、inbox、WAL journaling、crash-restore の仕組みは、チャットセッションが使うものとまったく同じです — driver は単に「ターン」を LLM に通すユーザー発話としてではなく、run/resume の nudge として解釈するだけです。実務上の利点は、pipeline の crash-recovery が、他のあらゆるセッションにとってもともと正しくなければならないインフラの上に乗ることであり、そこからズレうる第二の recovery パスにはならないことです。

エージェントが起動した pipeline の run と関わる方法は二通りあります:

- **Sync / attached**(`run_pipeline`、`run_pipeline_inline`): 呼び出し元が driver-session の run に attach し、terminal 状態に達するのを in-band で待ちます — その間ライブなステップ進捗イベントが呼び出し元にストリームされ(TUI のライブビューが描画するもの)、協調的な Ctrl-C は run を途中で kill するのではなく、次のステップ境界でクリーンに停止させます。attach 中にプロセスがクラッシュしても run 自体は失われません — recovery が resume し、結果は代わりに後で inbox メッセージとして届きます。
- **Async / detached**(`run_pipeline_async`、`run_pipeline_inline_async`): 呼び出し元は即座に `{status: started, run_id}` を受け取り、結果は run が terminal 状態に達した時点で後から inbox メッセージとして届きます。

## Crash recovery

Crash-recovery は、同じ手順を毎回エージェントに再計画させるのに対する pipeline 機能の差別化要因です: pipeline の run は、既に副作用を起こしたステップを再実行することなく、中断した箇所からちょうど resume できます。

これを実現する部品は二つです:

- **run 単位の work order**(`invocation.json` として永続化)。最初のステップが走る*前に*書き込まれます。何もない状態から run を再構成するのに必要なもの一式を運びます — pipeline 定義そのもの(そのため resume は ad-hoc な inline pipeline であっても外部レジストリを一切必要としません)、seed input、reply address、`verify: schema` ステップが検証対象とするスキーマ、などです。
- **ステップ境界での generation スナップショット**、各ステップ完了後に記録されます: run の pipe data、named store、そして既に完了したステップの集合です。resume は最新のスナップショットを読み、既に完了として記録されているすべてのステップをリプレイします — `call` / `match` / `fold` / `for_each` の内部進捗も含みます(それぞれ自身のサブステップをネストしたキーの下に記録します)。そのため合成の途中でのクラッシュが、既に着地した副作用を再発火させることはありません。したがって recovery は **exactly-once execution** です: ステップの副作用は、run が何度 resume されても一度だけ発火します。対照的に、*最終結果*の reply address への配送は **at-least-once** です — 最後のステップの完了と結果の post の間でクラッシュすると、次の recovery パスで再配送されます。呼び出し元が結果を静かに失うことは決してない代わりに、重複配送を許容する必要があります。

pipeline 定義がどのようにしてセッションに届くかは [Pipeline registration](pipeline-registration.md) を、規範的なステップ / プリミティブ文法と 4 つの起動ツールは [Pipeline DSL リファレンス](../../reference/runtime/pipeline-dsl.md) を参照してください。

## 現時点でスコープ外のもの

ディスク上の `pipelines/` の変更を実行中セッションの pipeline レジストリにホットリロードする機能は、まだ構築されていません。実行中または完了した pipeline run の rewind/fork セマンティクスは、上述の crash-recovery の exactly-once 保証とは別の、先送りされた関心事です。

## 関連ドキュメント

- [Pipeline registration](pipeline-registration.md) — pipeline 定義がどのようにディスクから読み込まれ、起動可能になるか。
- [Pipeline DSL リファレンス](../../reference/runtime/pipeline-dsl.md) — 規範的なステップ / プリミティブ文法、expression 言語、起動ツール。
- [Capability profiles](capability-profile.md) — pipeline 起動を制限する capability floor。
