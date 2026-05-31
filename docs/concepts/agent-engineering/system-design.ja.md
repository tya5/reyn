---
type: concept
topic: architecture
audience: [human, agent]
---

# System Design

agent システムのマクロ構造: 制御フロー、状態、責任をレイヤーを横断してどのように分散させるか、そしてランタイムが LLM が何をしても強制する不変条件は何か。

## Reyn の実装方法

3 つのレイヤー、それぞれ単一の責任を持つ:

| レイヤー | 所有するもの | 知っていること |
|-------|------|-------------|
| **OS** | ランタイムループ、検証、Events | Skill のドメインは何も知らない |
| **Skill** | グラフ、エントリー Phase、final output schema | 自身の Phase と artifact のみ |
| **Phase** | 入力 artifact 型 + LLM への指示 | 自身の入力以外は何も知らない |

この分割から 2 つの不変条件が生まれます:

1. **グラフが LLM を制約する。** LLM は `candidate_outputs` からエッジを選択し、OS はそれ以外を拒否します。制御フローは有限状態機械であり、自由形式のプロンプトチェーンではありません。
2. **OS は Skill に依存しない。** 特定の Phase、artifact、またはフィールドを命名する文字列リテラルは OS コードに現れません（P7）。新しい Skill は純粋なデータであり、OS コードは変わりません。

結果として、ワークフローの動作は Skill ファイルと各 Phase 内での LLM の選択の関数になります。どちらも小さく、検査可能です。

### LLM の制御フローを制限する理由

制限のない LLM オーケストレーションは、測定可能な 3 つの方法で不安定です:

- **ドリフト。** 自由な選択のたびにタスクから逸脱するチャンスが生まれます。
- **テスト不可能性。** 「このプロンプトは最終的に完了するか？」は自由な agent では決定不可能ですが、有限グラフでは自明に決定可能です。
- **再入不可能。** 何かが失敗した場合、失敗した Phase を特定したいものです。自由形式のオーケストレーションには指し示す Phase がありません。

Reyn は Skill グラフを明示的に書くコストを支払い、代わりに予測可能性を得ます。

## まだ薄い部分

グラフは自己ループのない DAG です。Phase は自身を次の Phase としてリストできません。修正ループは別の Phase を使います（`review → revise → review`）。実用上は問題ありませんが、一部のフレームワークが「同じステップをインラインでリトライ」できるのに対し、1 つのノードが追加されます。単一の Phase では不十分な場合のエスケープハッチは、サブ Skill ノード（グラフ内の `@subskill`）です。

## 関連情報

- [../architecture/principles.md](../architecture/principles.md) — P1、P2、P3、P7
- [../architecture/phase-vs-skill-vs-os.md](../architecture/phase-vs-skill-vs-os.md)
- [../architecture/llm-as-decision-engine.md](../architecture/llm-as-decision-engine.md)
- [リファレンス: graph](../../reference/dsl/graph.md)
- [tool-contract-design.md](tool-contract-design.md) — LLM が選択をどのように表現するか
- [reliability-engineering.md](reliability-engineering.md) — LLM が間違えたときに何が起こるか
