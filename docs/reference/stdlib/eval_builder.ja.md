---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [eval_builder]
---

# `eval_builder`

Skill の eval スペック（`eval.md`）を自動生成します。

## エントリー

`analyze_skill`

## 最終出力

`eval_spec_result` — 生成された `eval.md` へのパス、ケース数、基準数、サマリー。

## 動作方法

ターゲット Skill の `skill.md` と Phase ファイルを読み取り、グラフを実行するテストケースを推測し、Phase ごとの品質基準を提案します。ユーザーは `reyn eval <eval_md_path>` でスペックを別途実行します。

## Phase が Python preprocessor を使用する場合

`eval_builder` は Phase が Python ステップを持つ場合、基準に DO/DON'T テンプレートを書きます。これにより、「char_count が正しい」のような LLM judge が実際には検証できない「自明に真」な基準を避けられます。

## 例

```bash
reyn run eval_builder "build an eval for my_explainer"
```

## ソース

[`src/reyn/stdlib/skills/eval_builder/skill.md`](https://github.com/tya5/reyn/blob/main/src/reyn/stdlib/skills/eval_builder/skill.md)
