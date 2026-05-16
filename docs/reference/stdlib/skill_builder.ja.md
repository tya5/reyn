---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [skill_builder]
---

# `skill_builder`

自然言語の説明から新しい Skill を生成します。

## 目的

プランニング、artifact の設計、DSL ファイルの作成、結果のリント。失敗した場合はオプションで修正します。

## エントリー

`plan_skill`

## 最終出力

`skill_builder_result` — 名前、パス、書き込んだファイル、リント結果。

## 使うべき状況

- Skill のアイデアがはっきりしているが、DSL を手書きしたくない。
- シンプルな線形または分岐フローを持つ 2〜5 Phase の Skill が欲しい。

## 使うべきでない状況

- 既に近い Skill がある — 代わりに [skill_improver](skill_improver.md) を使う。
- 別のフレームワークからインポートする — [skill_importer](skill_importer.md) を使う。

## 例

```bash
reyn run skill_builder "A skill that takes a topic and returns a one-paragraph explainer. Two phases: outline (3 bullets) then expand (paragraph)."
```

## ソース

[`src/reyn/stdlib/skills/skill_builder/skill.md`](https://github.com/tya5/reyn/blob/main/src/reyn/stdlib/skills/skill_builder/skill.md)
