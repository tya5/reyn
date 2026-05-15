---
type: reference
topic: dsl
audience: [human, agent]
applies_to: [skill.md]
---

# `skill.md` frontmatter

すべての Skill は `skill.md` を含むディレクトリです。その YAML frontmatter が Skill の構造を宣言します。

## スキーマ

```yaml
---
type: skill                    # 常に "skill"
name: my_skill                 # 一意の識別子
description: One-line summary  # `reyn skills` に表示
entry: <phase_name>            # 必須; 最初に実行される Phase
final_output: <artifact_type>  # 必須; Skill の結果のスキーマ
final_output_description: |    # 省略可能; 人が読める結果の説明
  ...
finish_criteria:               # 省略可能; クリーンな終了のための条件
  - All inputs validated
  - Final output passes the quality bar
graph:                         # 必須; 許可されるトランジション
  outline: [expand]
  expand: [end]
permissions:                   # 省略可能; 必要なケイパビリティを宣言
  shell: deny
  python:
    - module: stats
      function: compute
      mode: safe
imported_from: ...              # 省略可能; `skill_importer` が設定する出自情報
imported_at: 2026-04-29T...
imported_format: claude-skill
imported_revision: <git-sha>
---
```

## 必須フィールド

- **`type`** — `skill` でなければなりません。
- **`name`** — 解決とイベント相関に使用されます。
- **`entry`** — 開始する Phase の名前。`phases/` に存在しなければなりません。
- **`final_output`** — Skill が完了したときに生成される artifact 型。`artifacts/<name>.yaml` または stdlib の artifact として定義されている必要があります。
- **`graph`** — 隣接リスト。各キーは Phase 名、各値は許可される次 Phase 名のリスト。末端トランジションのマークには `end` を使用します。

## 省略可能なフィールド

- **`description`** — `reyn skills` に表示されます。
- **`final_output_description`** — Skill 詳細に表示される長い説明。
- **`finish_criteria`** — 終了が許可されるタイミングを Phase が知るために使用されます。
- **`permissions`** — `reference/config/permissions.md` を参照してください。
- **`imported_*`** — `skill_importer` が書き込む出自フィールド。非アクティブ; パーサーはこれらを無視します。
- **`search_hints`** — 省略可; このスキルが答えられる例示クエリのリスト。カタログがルーターのコンテキストウィンドウを超える際の BM25/embedding 事前フィルタに使用される。大規模マルチスキルリポジトリでの recall 向上目的。
  例: `search_hints: ["記事を要約して", "tl;dr"]`

## ボディ

frontmatter の後、Markdown ボディは Skill の散文による説明です: 何をするか、いつ使うか、例。`reyn skills <name>` で表示されます。

## バリデーション

`reyn lint <skill_name>` がチェックします:

- `graph` で参照されるすべての Phase が `phases/` に存在する。
- `entry` が `graph` のキーである。
- `final_output` が `artifacts/` または stdlib の artifact に一致する。
- Phase の artifact 参照が解決可能である。
- Python preprocessor ステップ（ある場合）が `permissions.python` に一致し、対応する `.py` ファイルが存在する。

## 例

```yaml
---
type: skill
name: my_explainer
description: Generate a one-paragraph explainer from a topic.
entry: outline
final_output: explainer
graph:
  outline: [expand]
  expand: [end]
---

# my_explainer

`topic_input` artifact を受け取り、フレンドリーで例豊富な
1 段落の説明文を生成します。2 つの Phase: `outline` が 3 つの
箇条書きを生成し、`expand` がそれらを散文に変換します。
```

## 関連情報

- [phase-md.md](phase-md.md) — Phase frontmatter
- `reference/dsl/artifact-yaml.md` — artifact スキーマファイル
- `reference/dsl/graph.md` — グラフセマンティクスの詳細
- [コンセプト: P2 Skill が構造を定義する](../../concepts/principles.md#p2-skill-defines-structure)
