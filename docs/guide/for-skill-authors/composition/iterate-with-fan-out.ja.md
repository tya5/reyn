---
type: how-to
topic: dsl
audience: [human]
applies_to: [phases/*.md]
---

# リストに対してサブ Skill を fan-out する

**目的:** リスト内の各アイテムに対して同じサブ Skill を一度実行し、結果を決定論的に収集してから LLM に渡す。

## 使うべき状況

- N 個の入力があり、N 個の独立した決定が必要な場合（例: N の基準を採点、N のドキュメントを要約）。
- 各アイテムが独立している — あるアイテムは他のアイテムの出力を必要としない。
- LLM にループをオーケストレートさせるのではなく、安定した再現可能なパイプラインが欲しい。

## パターン

```yaml
---
type: phase
name: judge_all_criteria
input: phase_eval_request_batch
preprocessor:
  - iterate:
      over: phase_eval_requests
      apply:
        run_skill:
          skill: judge_phase
          input:
            type: phase_eval_request
            data: ${item}
      into: phase_judgments
      on_error: fail
---

`phase_judgments` を総合的な判定に集約してください。
```

処理の流れ:

1. OS は Phase 入力の `phase_eval_requests` にある配列を読み取ります。
2. 各 `${item}` に対して、`judge_phase` を完了まで呼び出し、`final_output` を収集します。
3. 収集されたリストが `input.phase_judgments` に格納されます。

## `on_error`

| 値 | 動作 |
|-------|----------|
| `fail`（デフォルト） | 最初のサブ Skill 失敗でイテレーションを止め、エラーを伝播する |
| `skip` | 失敗したアイテムは結果リストから除外され、イテレーションを続ける |

部分的な結果でも有用な場合（eval レポート、バッチサマリー）は `skip` を使い、1 つの不良アイテムで中止すべき場合は `fail` を使います。

## `apply` に入れられるもの

MVP では `run_skill` のみをサポートします。アイテムごとに他のことをしたい場合は、そのことを行うサブ Skill を作成し、それに対してイテレートします。

## 実際の例: `eval`

stdlib の `eval` Skill は、基準ごとのリクエストに対して `judge_phase` をイテレートします:

```yaml
preprocessor:
  - iterate:
      over: phase_eval_requests
      apply:
        run_skill:
          skill: judge_phase
          input: { type: phase_eval_request, data: ${item} }
      into: phase_judgments
```

採点 Phase は `phase_judgments` を読み取り、集約します。

## 関連情報

- [リファレンス: preprocessor](../../../reference/dsl/preprocessor.md) — `iterate` ステップ
- [compose-skills-with-run-skill.md](compose-skills-with-run-skill.md)
- [リファレンス: stdlib/eval](../../../reference/stdlib/eval.md)
