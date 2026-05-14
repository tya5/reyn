---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn lint]
---

# `reyn lint`

Skill ディレクトリに対して決定論的な構造チェックを実行します: グラフ、frontmatter、artifact 参照、Python preprocessor ステップ（`mode: safe` の場合は AST も検証）。ほとんどのオーサリングミスをランタイム前に検出します。

## 概要

```
reyn lint SKILL
```

## 位置引数

| 名前 | 説明 |
|------|-------------|
| `SKILL` | Skill 名。[`reyn run`](run.md) と同じ解決順序: `reyn/project/` → `reyn/local/` → stdlib。 |

## チェックされる内容

- **グラフ**: すべてのキーが `phases/` の Phase ファイルを参照している; すべての値が既知の Phase、サブ Skill（`@name`）、または `end` である。
- **到達可能性**: `entry` から到達可能なすべての Phase; `can_finish: true` の Phase が `end` へのパスを持つ。
- **Frontmatter**: 必須キー（`type`、`name`、`entry`、`final_output`）。
- **artifact 参照**: すべての `input` と `final_output_schema` が artifact ファイルに解決される。
- **Preprocessor**: 各 `python` ステップに一致する `permissions.python` エントリーがあり、`.py` ファイルが存在し、関数が定義されている。`mode: safe` では AST が allowlist に対してチェックされます（`open`、`eval`、`exec`、`__import__`、`subprocess` などは禁止）。

## 終了コード

| コード | 意味 |
|------|---------|
| `0` | エラーなし（警告はある場合あり） |
| `1` | 1 つ以上のエラーが見つかった |

## 出力

各問題は独自の行に表示されます:

```
[error]   reyn/local/my_skill/phases/draft.md
          graph references unknown phase 'reveiw' (typo for 'review'?)

[warning] reyn/local/my_skill/phases/draft.md
          phase 'draft' has can_finish: true but no path to 'end'
```

サマリーが続きます: `N error(s), M warning(s)`。

クリーンな場合: `No issues found.`

## 例

プロジェクト Skill をリント:

```bash
reyn lint article_writer
```

stdlib Skill をリント（編集後の健全性チェック）:

```bash
reyn lint eval
```

CI での使用:

```bash
reyn lint my_skill || exit 1
```

## 関連情報

- [リファレンス: skill.md](../dsl/skill-md.md)
- [リファレンス: phase.md](../dsl/phase-md.md)
- [リファレンス: graph](../dsl/graph.md)
- [リファレンス: preprocessor](../dsl/preprocessor.md)
