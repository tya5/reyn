---
type: tutorial
topic: getting-started
audience: [human]
---

# 02 — はじめての skill を作る

`skill_builder` を使って、エンドツーエンドで動く skill を作ります。最後には、トピックを受け取って一段落の解説を返す skill が完成します。

## 作るもの

```
reyn run my_explainer "machine learning"
→ "Machine learning は ..."
```

2 phase の skill：

1. `outline` — 3 つの bullet point を生成
2. `expand` — outline を一段落の文章に展開

## Step 1: skill のスキャフォルディング生成

```bash
reyn run skill_builder "トピックを受け取って一段落の解説文を返す skill。phase は outline（3 bullet）と expand（段落）の 2 つ。"
```

`skill_builder` は stdlib skill です。構造を計画し、artifact を設計し、phase の markdown を生成し、結果を lint し、必要なら修正してから返します。出力先は `reyn/local/my_explainer/`（ディレクトリ名は計画段階で決まる — あとから rename 可能）。

## Step 2: 生成物の確認

```
reyn/local/my_explainer/
├── skill.md
├── phases/
│   ├── outline.md
│   └── expand.md
└── artifacts/
    ├── topic_input.yaml
    ├── outline.yaml
    └── explainer.yaml
```

`skill.md` を開いて `graph:` と `final_output:` を確認。`phases/outline.md` を開いて `input:` と instructions だけが書かれていて、output schema が無いことを確認します。

この分離は reyn の核心です。理由は [concepts/principles.md](../../concepts/principles.md) を参照。

## Step 3: 実行

```bash
reyn run my_explainer "光合成"
```

`--events` を付けると裏側の状態遷移が見えます：

```bash
reyn run my_explainer "光合成" --events
```

各 phase が `phase_started`, `llm_called`, `artifact_created`, `phase_completed` を発行します。完全な event ログは `reyn events <log_file>` で再生可能です。

## Step 4: 反復改善

出力が望むものでなければ `skill_improver`：

```bash
reyn run skill_improver "my_explainer の出力が学術的すぎる。フレンドリーで例の多い文体にしたい"
```

skill を読み、変更を計画し、diff を提示します。ファイル書き込み前に必ずユーザ承認を求めます。

## 学んだこと

- **Skill はディレクトリ** — markdown と YAML の集まり、Python コードではない
- **Phase は input だけを宣言** — output は次 phase または skill の `final_output` で決まる
- **skill を作るのも skill** — `skill_builder` や `skill_improver` は特別なツールではなく、普通の stdlib skill

## 次のステップ

- [ハウツー: 自作 skill をゼロから書く](../for-skill-authors/write-your-first-custom-skill.md) — 同じ形を手書きで構築し、各ファイルの役割を理解する（英語版にフォールバック）
- Tutorial 03 — Running a skill（Phase 2）
- Tutorial 04 — Writing an eval（Phase 2）
- [Reference: skill.md frontmatter](../../reference/dsl/skill-md.md)（英語版にフォールバック）
