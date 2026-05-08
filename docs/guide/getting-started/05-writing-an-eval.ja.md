---
type: tutorial
topic: getting-started
audience: [human]
---

# 05 — eval を書く

Eval は「出力が良さそうだった」を「出力が M ケースにわたって N 基準を通過した」に変えます。このチュートリアルでは `my_explainer`（[チュートリアル 03](03-your-first-skill.md) の Skill）のルーブリックを構築して実行する方法を説明します。

## eval の形式

eval スペックは frontmatter と 1 つ以上のケースを持つ Markdown ファイルです:

```markdown
---
skill_dsl_path: my_explainer
model: standard
---

# Case: short_topic

input: photosynthesis

## Phase: outline
- 各箇条書きは完全な文である。
- 箇条書きは異なる角度をカバーする（重複なし）。

## Phase: expand
- 段落はすべての 3 つの箇条書きに言及している。
- トーンはフレンドリーで、学術的でない。
```

各 `## Phase: <name>` ブロックはルーブリック基準をリストします。`eval` Skill は `judge_phase` を使用して各基準を Phase ごとに採点します。

## ステップ 1: ドラフトを生成する

```bash
reyn run eval_builder "build an eval for my_explainer covering tone and structure"
```

`eval_builder` は Skill を読み取り、ケースと基準のドラフトを作成し、`reyn/local/my_explainer/eval.md` に書き込みます。

## ステップ 2: レビューする

ファイルを開きます。調整します:

- **ケース** — エッジケースを追加します（空のトピック、非常に長いトピック、曖昧なトピック）。
- **基準** — 曖昧なものをテスト可能な文に絞り込みます。「段落が良く書かれている」は確実に採点できません。「段落が 2-4 文である」は確実に採点できます。

## ステップ 3: 実行する

```bash
reyn eval reyn/local/my_explainer/eval.md
```

出力:

```
=== Eval: my_explainer  [3 case(s)] ===
    model=standard

━━━ case: short_topic ━━━
  input: photosynthesis
  ✓ score=0.95  (4/4 required)

━━━ case: long_topic ━━━
  ...

═══════════════════════════════════════════════════
 ✓ 3/3 cases passed
 Results → .reyn/eval_reports/my_explainer/<timestamp>.json
═══════════════════════════════════════════════════
```

`reyn eval` はステータス 0（全通過）、1（スペックの読み込み失敗）、または 2（ケース失敗）で終了します。

## ステップ 4: ルーブリックを絞り込む

基準が「不正な出力でも通過する」場合、十分に具体的ではありません。失敗したケースを確認します:

```bash
cat .reyn/eval_reports/my_explainer/<timestamp>.json
```

失敗した各基準について、レポートには judge の推論が含まれます。それを使って基準をより具体的に書き直し、再実行します。

## eval は非インタラクティブ

`reyn eval` はプロンプトを表示しません。ターゲット Skill が必要とする Permission はすべて事前承認されている必要があります（`reyn.yaml` の `permissions:` またはこれまでの `reyn run` から `.reyn/approvals.yaml` に保存済み）。事前承認がなければ、ケースは未完了として報告されます。[manage-permissions](../for-users/manage-permissions.md) を参照してください。

## 学んだこと

- eval スペックは Markdown の Phase をキーとしたルーブリックです。
- `eval_builder` がドラフトを生成し、あなたがレビューしてイテレートします。
- `reyn eval` はすべてのケースを非インタラクティブに実行してレポートを書き込みます。

## 次へ

- [チュートリアル 02 — Chat モード](02-chat-mode.md) — まだ見ていなければ。
- [リファレンス: stdlib/eval](../../reference/stdlib/eval.md)
- [リファレンス: stdlib/eval_builder](../../reference/stdlib/eval_builder.md)
