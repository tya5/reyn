---
type: concept
topic: architecture
audience: [human, agent]
---

# Skill design patterns — ほとんどの Skill が取る 3 つの形

最初の Skill を作り終えたら（[Write your first custom skill](../guide/for-skill-authors/foundation/write-your-first-custom-skill.md) 参照）、次の疑問は「2 つ目を設計するとき、どんな形にすべきか？」です。Reyn の Skill のほとんどは 3 つのパターンのいずれかに当てはまります。複雑さのためではなく、Skill がやるべきことで選んでください。

## Pattern 1: Linear (read → process → write)

**形:** Phase が単一のチェーンでつながり、サイクルも分岐もありません。各 Phase の LLM には許可された次の Phase が 1 つだけあります。

```
graph:
  A: [B]
  B: [C]
  C: [end]
```

```
A --> B --> C --> end
```

**使い時:** 各 Phase が下流の Phase へ明確に引き渡せる場合。作業がパイプライン（入力収集 → メイン処理 → フォーマットして配信）で表現できる場合。

**Stdlib の例:**

- `direct_llm` — 最もシンプルなケース。すぐに finish する単一 Phase。Graph: `{ respond: [] }`。Preprocessor なし、分岐なし、LLM 呼び出し 1 回。
- `word_stats_demo` — 単一 Phase に Python preprocessor を加えた構成。LLM 呼び出し前にテキスト統計を計算します。Graph: `{ review: [] }`。Linear 形の中で決定論的分離（deterministic split）原則を示します。
- `read_local_files` — 2 Phase の Linear パイプライン。Graph: `{ decide_files: [read_and_respond], read_and_respond: [] }`。`decide_files` の LLM がファイル読み取り op を発行し、`read_and_respond` へ遷移して最終回答を生成します。
- `skill_importer` — 3 Phase の Linear パイプライン。Graph: `{ search: [select], select: [convert], convert: [] }`。

**トレードオフ:** 理解しやすいが柔軟性が低い。LLM が次の Phase では消化できない出力を生成しても、復旧ループがありません。不正な出力はバリデーションエラーとして abort されます。

## Pattern 2: Loop (generate → review → refine)

**形:** グラフ内の Phase が複数の次 Phase を持ちます。前進（十分な品質 → deliver）することも後退（改善が必要 → 再度 refine）することもできます。サイクルはグラフに明示的に宣言され、ファーストクラスです。

```
graph:
  generate: [review]
  review:   [generate, finalize]
  finalize: [end]
```

```
generate --> review --(改善が必要)--> generate
                   \--(十分な品質)--> finalize --> end
```

Skill グラフのサイクルは違反ではありません。意図的な機能です。構文については [reference/dsl/graph.md](../reference/dsl/graph.md) を参照してください。

ループは **OS rollback** によっても実現できます。Phase が `control.type="rollback"` を emit すると、OS はフィードバックをコンテキストに注入して以前の Phase を再実行します。グラフに後退エッジを追加せずに反復的な挙動を維持できます。どちらのメカニズムも外側から見た観測可能な効果は同じです。

**使い時:** 出力品質にばらつきがある場合。1 回のパスで確実な品質が得られない場合。バリデーションエラーでなく判断された品質に基づく制限付きリトライが必要な場合。コンテンツ生成、コード生成、反復的な計画立案に多く使われます。

**Stdlib の例:**

- `skill_builder` — OS rollback ループを持つ 5 Phase の Linear グラフ。Graph: `{ plan_skill: [design_artifacts], design_artifacts: [review_plan], review_plan: [build_skill], build_skill: [verify_skill] }`。`verify_skill` Phase は `reyn lint` を実行し、失敗した場合は lint の問題をフィードバックとして `build_skill` を再実行する rollback を emit します。ループは `max_phase_visits` で制限されます。
- `skill_improver` — 長いループ Skill。Graph: `{ prepare: [copy_to_work], copy_to_work: [run_and_eval], run_and_eval: [plan_improvements], plan_improvements: [apply_improvements], apply_improvements: [finalize] }`。`apply_improvements` は次のイテレーションのために `run_and_eval` にロールバックするか、停止条件が満たされると `finalize` へ遷移します。ループ終了条件（スコアしきい値、最大イテレーション数、後退、停滞）は Skill の `skill.md` に記載されています。

**トレードオフ:** 強力だが、サイクルには信頼できる finish パスが必要です。終了条件がなければ LLM は「さらに改善が必要」と無限に判断し続けます。`phase.max_visits`、Skill レベルのイテレーション上限、または終端 Phase に強制遷移する明示的な停止条件でループを制限してください。

## Pattern 3: Sub-skill composition (delegation)

**形:** Phase が `run_skill` Control IR op を通じて別の Skill をサブスキルとして呼び出します。サブスキルは最後まで実行され、その `final_output` artifact が親の Workspace に流れ込みます。親のグラフ形状は変わりませんが、1 つの Phase の `allowed_ops` に `run_skill` が含まれます。

```
graph:
  prepare: [execute]    # execute Phase が run_skill op を発行
  execute: [aggregate]
  aggregate: [end]
```

```
prepare --> execute --(run_skill)--> [サブスキルが実行される] --> execute --> aggregate --> end
```

サブスキルはグラフノードとして `@sub_skill` プレフィックスを使って宣言することもできます。両方のフレーバーについては [Compose skills with run_skill](../guide/for-skill-authors/composition/compose-skills-with-run-skill.md) を参照してください。

**使い時:** 作業がすでに Skill として存在する（またはそうなり得る）自己完結したサブタスクを含み、親のグラフを小さく保ちたい場合。サブタスクの出力を既存の artifact スキーマに対して検証してから親が進む必要がある場合にも有効です。

**Stdlib の例:**

- `eval` — `run_target` Phase がテスト対象の Skill を呼び出すために `run_skill` Control IR op を発行します。Graph: `{ run_target: [evaluate] }`。`run_target` の `allowed_ops: [run_skill]` 宣言がこれを許可します。サブスキルの出力はその後、判定のために `evaluate` Phase へ渡されます。
- `skill_improver` — `run_and_eval` Phase が `run_skill` を通じて `eval` Skill を呼び出します（`skill_improver/skill.md` に「`eval` および `eval_builder` スキルを `run_skill` Control IR op 経由で呼び出す」と記載）。
- Preprocessor `run_skill` — Phase は LLM より前の preprocessor ブロックで、決定論的にサブスキルを呼び出すこともできます。チャットルーターでの `recall_memory` の利用がこの形です（[Compose skills with run_skill](../guide/for-skill-authors/composition/compose-skills-with-run-skill.md) 参照）。

**トレードオフ:** Composition により各 Skill のグラフがシンプルになり、テスト済みサブスキルの再利用が促進されます。コストはランタイム依存性です。サブスキルが存在しないか、そのコントラクトが変わると、親が壊れます。`reyn lint` で検証してください。

## パターンの組み合わせ

実際の Skill は 3 つのうち 2 つを組み合わせることが多いです。

- **Linear + Sub-skill:** 1 つの Phase がサブスキルに委任する Linear パイプライン。`eval` がこの形です。Linear グラフで、1 つの Phase が `run_skill` を発行します。
- **Loop + Sub-skill:** 各イテレーションでサブスキルを呼び出すループ Skill。`skill_improver` がこの形です。OS rollback ループで、`run_and_eval` が各イテレーションで `eval` サブスキルを呼び出します。
- **Multi-agent（Layer 3 および 4）** はこれらと直交します。3 つのパターンのどれでも、別のエージェントに委任するエージェントの内部で使えます。より広い全体像は [multi-agent.md](multi-agent.md) を参照してください。

3 つのパターンをすべて 1 つの Skill に組み合わせることは警告サインです。通常、その Skill がやりすぎていることを意味します。

## 避けるべきアンチパターン

- **過剰な分解。** 3 つで済むところを 8 Phase にする。Phase の境界ごとにコンテキスト構築のコストがかかります。デフォルトは Phase 数を少なく。Phase の指示内容や input schema が実質的に異なるときだけ分割してください。

- **finish パスのないサイクル。** LLM が「さらに改善が必要」と無限に判断し続けます。`max_phase_visits` 制限、Phase 指示でチェックするイテレーションカウンター、または品質にかかわらず終端 Phase へ遷移する明示的な停止条件を常に設けてください。

- **サブスキルの乱用。** 親グラフを「きれいに見せる」ためにすべてをサブスキルにする。サブスキルはルックアップのオーバーヘッドと依存関係サーフェスをもたらします。既存の再利用可能なサブスキルを組み合わせるのは安価ですが、将来の再利用を見越した投機的なサブスキルは割高です。

- **判断なしの分岐。** `{ A: [B, C, D] }` のように LLM がどの分岐を選ぶか確実に判断できないグラフ。分岐が意味を持つのは、入力が明確にパスを識別できる場合です（適切に動機付けされた分岐の例は `skill_builder` のパターンテーブルを参照）。LLM が確実に適用できるほど明確でない選択基準の分岐は避けてください。

## See also

- [principles.md](principles.md) — P1 と P2: これらのパターンが体現する設計時の不変条件（Phase は input のみを宣言する; Skill がグラフを所有する）。
- [architecture.md](architecture.md) — コンポーネント全体のレイヤー構造。
- [multi-agent.md](multi-agent.md) — Layer 3 と 4、これらの 3 つのパターンと直交します。
- [Write your first custom skill](../guide/for-skill-authors/foundation/write-your-first-custom-skill.md) — これらのパターンを実践で適用する。
- [Compose skills with run_skill](../guide/for-skill-authors/composition/compose-skills-with-run-skill.md) — Pattern 3 の詳細。
- [reference/dsl/graph.md](../reference/dsl/graph.md) — グラフ構文とサイクルのセマンティクス。
