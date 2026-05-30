---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn skills]
---

# `reyn skills`

インストール済みの Skill の一覧表示、使用方法の詳細確認、または op/permission クロスレイヤー整合性の検証を行います。

## 概要

```
reyn skills
reyn skills <SKILL_NAME>
reyn skills validate <SKILL_NAME>
reyn skills validate --all
```

## サブコマンド / 形式

| 形式 | 説明 |
|------|------|
| `reyn skills` | インストール済みの全 Skill を一覧表示（project → local → stdlib）。 |
| `reyn skills <SKILL_NAME>` | 1 つの Skill の使用方法の詳細を表示 — 説明、entry Phase、final output、本文。 |
| `reyn skills validate <SKILL_NAME>` | 1 つの Skill の op/permission クロスレイヤー整合性を検証。 |
| `reyn skills validate --all` | インストール済みのすべての Skill を検証してサマリーを表示。 |

## `reyn skills` — 一覧表示

project（`reyn/project/`）、local（`reyn/local/`）、stdlib の各 Skill ディレクトリにわたる全インストール済み Skill の一覧を表示します。列: 名前、ソース、1 行説明。

```
NAME                SOURCE    DESCRIPTION
direct_llm     stdlib    Summarise text into a compact paragraph
article_writer      project   Draft and review a long-form article
```

## `reyn skills <SKILL_NAME>` — 詳細

1 つの Skill の完全な使用情報を表示します:

```
skill: article_writer (project)
entry:        draft
final_output: article

[skill.md のボディ / 説明]
```

解決順序: `reyn/project/` → `reyn/local/` → stdlib。

## `reyn skills validate` — op/permission 整合性チェック

Skill の各 Phase が `allowed_ops` で宣言する **Tier 2-3 op** に対応する `skill.permissions` エントリーが存在するか、またその逆（宣言された permission がどの Phase でも参照されていないか）を確認します。

チェック内容:

- **宣言なし Permission**: Phase が `allowed_ops` に Tier 2-3 op 種別（例: `shell`、`mcp`）を列挙しているが、Skill の `permissions:` ブロックにエントリーがない → **エラー**。
- **未使用 Permission**: Skill が permission を宣言しているが、どの Phase も当該 op 種別を参照していない → **警告**。

Tier 0-1 op（`ask_user`、`run_skill`、`web_search`、`web_fetch` など）は除外されます — 宣言不要です。

### 終了コード

| コード | 意味 |
|------|------|
| `0` | エラーなし（警告はある場合あり）。 |
| `1` | 1 つ以上のエラー、またはスキルが見つからない。 |

### 出力

単一スキルの実行:

```
Skill 'my_skill': OK — no cross-layer inconsistencies.
```

問題がある場合:

```
[error]   my_skill
          phase 'draft' has allowed_ops=[shell] but skill.permissions has no 'shell' entry

[warning] my_skill
          skill.permissions declares 'mcp:[github]' but no phase lists 'mcp' in allowed_ops
```

全スキル検証のサマリー:

```
Validated 12 skill(s). 1 error(s) in 1 skill(s), 2 warning(s) in 2 skill(s).
```

### 例

```bash
# 1 つの Skill を検証
reyn skills validate article_writer

# インストール済みのすべての Skill を検証（CI での使用に便利）
reyn skills validate --all || exit 1
```

## 関連情報

- [リファレンス: skill.md](../dsl/skill-md.md) — `permissions:` と `allowed_ops` フィールド
- [リファレンス: phase.md](../dsl/phase-md.md) — `allowed_ops` フィールド
- [リファレンス: lint](lint.md) — `reyn lint`（グラフ + artifact チェック；補完的）
- [コンセプト: permission-model](../../concepts/permission-model.md) — Tier 0-3 op 分類
