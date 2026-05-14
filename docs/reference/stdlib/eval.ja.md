---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [eval]
---

# `eval`

LLM-as-judge として `judge_phase` を使用して、ターゲット Skill を単一のテストケースに対して評価します。

## エントリー

`run_target`

## 最終出力

`eval_result` — 全体の合否、スコア、基準ごとのサマリー、最も弱い Phase。

## 構成方法

`evaluate` Phase は `iterate × run_skill` preprocessor を使用して、基準ごとの eval リクエストに対して `judge_phase` を fan-out します。LLM は基準ごとの判定を集約するだけです。イテレーション自体は決定論的な OS コードです。

## 注意事項 — Python preprocessor の承認

ターゲット Skill が Python preprocessor ステップを使用する場合、**各ステップは eval の前に承認されている必要があります**。eval は非インタラクティブな Permission リゾルバーで `run_skill` を通じてターゲットを呼び出します。eval 時にプロンプトはありません。

事前承認の 2 つの方法:

1. 最初にインタラクティブでターゲットを一度実行します（`reyn run <target> "<sample>"`）。承認が `.reyn/approvals.yaml` に保存されます。
2. `reyn.yaml` にプロジェクト全体の許可を設定します:

   ```yaml
   permissions:
     python:
       safe: allow
       unsafe: allow   # --allow-untrusted-python も必要
   ```

事前承認がない場合、ターゲットのランは失敗し、ケースは未完了として報告されます。

## 使用方法

`eval` は通常 `reyn eval <spec.md>` を通じて間接的に呼び出され、複数のケースにわたって反復して結果を集約します。CLI リファレンスは `reference/cli/eval.md` を参照してください。

## ソース

[`src/stdlib/skills/eval/skill.md`](https://github.com/tya5/reyn/blob/main/src/stdlib/skills/eval/skill.md)
