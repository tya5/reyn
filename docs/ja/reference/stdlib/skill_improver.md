---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [skill_improver]
---

# `skill_improver`

既存の Skill を eval で実行し、失敗している基準に対して DSL の変更を計画・適用し、スコアが閾値を満たすか停止条件が発火するまで再評価を繰り返しながら反復的に改善します。

## エントリー

`prepare`

## 最終出力

`improvement_result` — スコアの推移、変更したファイル、終了理由。

## 使うべき状況

- Skill の eval がスコア閾値を下回っており、自動修正が必要な場合。
- 明確な失敗モード（特定の基準が失敗）があり、ピンポイントで修正したい場合。

## 使うべきでない状況

- 対象 Skill にまだ eval スペックがない — 先に [eval_builder](eval_builder.md) を実行してください。
- 新しい Phase の追加やグラフ変更など構造的な変更が必要な場合 — `skill_builder` がより適切です。

## 要件

- 対象 Skill に `eval.md` スペックが必要です。
- improver がサブプロセスを呼び出す場合は `--allow-shell` が必要になることがあります。

## 例

```bash
reyn run skill_improver "improve my_explainer" --allow-shell
```

## ソース

[`src/stdlib/skills/skill_improver/skill.md`](https://github.com/<org>/reyn/blob/main/src/stdlib/skills/skill_improver/skill.md)
