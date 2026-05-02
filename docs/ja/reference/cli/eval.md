---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn eval]
---

# `reyn eval`

eval スペックをターゲット Skill に対して非インタラクティブに実行します。各ケースはルーブリック基準に対して Phase ごとに採点されます。ケースごとの結果と全体のサマリーが `.reyn/eval_reports/` に書き込まれます。

## 概要

```
reyn eval [OPTIONS] FILE
```

## 位置引数

| 名前 | 説明 |
|------|-------------|
| `FILE` | eval スペック Markdown へのパス（例: `reyn/local/my_skill/eval.md`）。スペックは `skill_dsl_path` frontmatter フィールドでターゲット Skill を参照します。 |

## オプション

| フラグ | 説明 |
|------|-------------|
| `--model MODEL` | モデルクラス（`light`/`standard`/`strong`）または LiteLLM モデル文字列。**優先度:** CLI > スペック > `reyn.yaml`。 |
| `--dsl-root DIR` | ターゲット Skill の DSL ルートオーバーライド。デフォルトでは Skill パスから推論されます。 |
| `--output-language LANG` | eval Skill とターゲット Skill の両方に渡される出力言語コード。デフォルトは `reyn.yaml` から。 |
| `--max-phase-visits N` | ケースごとの単一 Phase 再訪問の上限。`0` = 無制限。デフォルトは `reyn.yaml` または `25`。 |

## 終了コード

| コード | 意味 |
|------|---------|
| `0` | すべてのケースが通過 |
| `1` | スペックの読み込みに失敗（例: 不正な eval.md） |
| `2` | 1 つ以上のケースが基準に失敗 |

## 出力

ケースごとのサマリー行が stdout に表示されます:

```
━━━ case: short_summary ━━━
  input: reyn is a workflow OS for LLMs.
  ✓ score=0.95  (4/4 required)
```

完全な構造化レポートが `.reyn/eval_reports/<target_skill>/<timestamp>.json` に書き込まれ、最終行にパスが表示されます。

## 非インタラクティブ制約

`reyn eval` はプロンプトを表示しません。ターゲット Skill が必要とするすべての Permission は事前承認されている必要があります:

- ターゲットをインタラクティブで一度実行（`reyn run <target> "<sample>"`）してプロンプトを受け入れる。選択は `.reyn/approvals.yaml` に永続化されます。または
- `reyn.yaml` にプロジェクト全体の付与を設定:

```yaml
permissions:
  python.pure: allow
  python.trusted: allow   # ランタイムの --allow-untrusted-python も必要
```

事前承認がない場合、ターゲットランは失敗し、ケースは未完了として報告されます。ターゲット Skill のバグのように見えますが、原因は承認の欠如です。

## 例

プロジェクト Skill にバンドルされた eval を実行:

```bash
reyn eval reyn/project/article_writer/eval.md
```

このランのみモデルをオーバーライド:

```bash
reyn eval reyn/local/my_skill/eval.md --model strong
```

開発中のイテレーション（安価なモデル、単一ケース）:

```bash
reyn eval reyn/local/my_skill/eval.md --model light
```

## 関連情報

- [run.md](run.md) — `reyn run`（基盤となる実行パス）
- [リファレンス: stdlib/eval](../stdlib/eval.md) — eval Skill が生成するもの
- [リファレンス: stdlib/eval_builder](../stdlib/eval_builder.md) — スペックファイルを生成
- [リファレンス: permissions](../config/permissions.md) — 事前承認のメカニズム
