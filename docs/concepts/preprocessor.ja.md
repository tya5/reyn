---
type: concept
topic: architecture
audience: [human, agent]
---

# Preprocessor

**Preprocessor** は、Phase で LLM が呼び出される前に実行される決定論的なステップのチェーンです。各ステップは入力 artifact を補強し、その結果は `into` で指定したキーに書き込まれます。LLM がコンテキストフレームを受け取る時点で、入力 artifact には LLM が推測するのではなく引用できる計算済みの事実がすでに含まれています。

構造的には [postprocessor](postprocessor.md) と対称です — ステップ型、`on_error` セマンティクス、パーミッションゲートはすべて同一です。異なるのは **発火位置** のみです。Preprocessor は Phase 開始時に発火し、Postprocessor は Skill 終了時に発火します。

## Why

### 決定論的な処理に LLM は不要

LLM は文字数の計算、行数の集計、トークン長の計測といったタスクに対して信頼性が低く、Python がマイクロ秒単位で正確に処理できます。LLM が推測（またはハルシネーション）する事実を事前計算しておくことは、エージェントエンジニアリングの基本パターンです。Reyn においてその計算の場が Preprocessor です。

これは **決定論的分離（deterministic split）** 原則です。ある出力が入力の純関数として導出可能であれば、LLM に再現させるのではなく決定論的に導出します。より広い文脈は
[system-design.md](agent-engineering/system-design.md) を参照してください。この原則は
[P3](principles.md#p3-os-controls-execution)（OS が実行を制御する）および
[P5](principles.md#p5-workspace-is-the-single-source-of-truth)（Workspace が唯一の信頼できる情報源）と整合します。

### トークンコストの削減と正確性の向上

Preprocessor が計算した事実は、LLM が推論する必要のある推測を一つ減らします。これにより LLM の責務は判断・統合・生成 — LLM が得意なこと — に絞られます。`stats.word_count = 847` がすでに artifact に入った状態で LLM に届く Phase は、単語数をインラインで推定させる Phase より正確な出力を生み出します。

### Phase の再利用性

`data.stats` を読む Phase は、`stats` がどのようにして生成されたかを知りません。同じ Phase を、Preprocessor が注入するデータを変えるだけで異なる Skill に再ターゲットできます。Phase の指示やスキーマを変更する必要はありません。これが [P1](principles.md#p1-phase-is-stateless-and-reusable) による Phase 再利用の保証です。

## ステップの種類（概要）

| ステップ型 | 用途 |
|------------|------|
| `run_skill` | サブ Skill を呼び出し、その最終出力を `into` に格納する |
| `iterate` | サブステップをリストに対してファンアウトし、結果を `into` に収集する |
| `validate` | JSON-Schema チェックを実行し、LLM が判断できるよう findings を渡す |
| `lint_plan` | プラン artifact に対して決定論的な構造チェック（循環検出・カバレッジ）を実行する |
| `python` | ユーザー提供の Python 関数をサンドボックス化した `pure` / `trusted` モードで呼び出す |

すべてのステップに共通する不変条件が 2 つあります。結果は `into` に書き込まれ、ステップは宣言順に実行されます（後のステップは前のステップが生成した値を参照できます）。

各ステップの完全な構文は
[reference/dsl/preprocessor.md](../reference/dsl/preprocessor.md) を参照してください。

## Preprocessor vs LLM に任せるか — 判断の目安

| 状況 | 適切な場所 |
|------|-----------|
| 文字数・行数・合計の計算 | Preprocessor (`python`) |
| メイン Phase の前に既知のサブ Skill を呼び出す | Preprocessor (`run_skill`) |
| リストに対するファンアウト | Preprocessor (`iterate`) |
| 「判断する前に検証したい」 | Preprocessor (`validate`) — findings を LLM に渡し、LLM が判断する |
| プランの構造的な健全性チェック | Preprocessor (`lint_plan`) |
| 開かれた判断・統合・生成 | LLM |

判断の基準となる問い: 「このステップの出力は入力の純関数か？」。Yes であれば Preprocessor が適切です。

## Postprocessor との対称性

| | Preprocessor | Postprocessor |
|---|---|---|
| 発火タイミング | Phase 開始時 | Skill 終了時 |
| 入力元 | 上流 Phase の出力 | LLM の finish artifact |
| 出力先 | Phase の `input_schema`（補強済み） | Postprocessor の `output_schema` |
| ステップ型 | `run_skill` / `iterate` / `validate` / `lint_plan` / `python` | 同一 |
| `on_error` ポリシー | ステップごとに `fail` / `skip` / `empty` | 同一 |
| パーミッションゲート | `skill.permissions` | 同一 |

ランナーは両者でロジックを共有しており、異なるのは流入する artifact、出力を検証するスキーマ、および発火位置です。

## 実例: `word_stats_demo`

`word_stats_demo` stdlib Skill は最もシンプルな標準的な例です。その `review` Phase は `python` Preprocessor ステップを一つ宣言しています。

```yaml
preprocessor:
  - type: python
    module: ./stats.py
    function: compute_text_stats
    into: data.stats
    output_schema:
      type: object
      properties:
        char_count:        {type: integer, minimum: 0}
        word_count:        {type: integer, minimum: 0}
        line_count:        {type: integer, minimum: 0}
        longest_line_chars: {type: integer, minimum: 0}
        estimated_tokens:  {type: integer, minimum: 1}
      required: [char_count, word_count, line_count, longest_line_chars, estimated_tokens]
```

LLM に何を与えるか: LLM 呼び出し前に `input_artifact.data.stats` が正確な計数値で埋まっています。Phase の指示は LLM に「少なくとも 1 つの統計値を字義通りに引用すること」を求めます — その数値が LLM 自身の推定ではなく Python から来ているため、これが確実に実現できます。

## エラーセマンティクス

Preprocessor ステップには `on_error: fail | skip | empty` を宣言できます。

- **`fail`**（デフォルト）: ステップ失敗は例外を発生させ Phase を中断します。
- **`skip`**: 失敗はログに記録され、後続ステップは続行します。
- **`empty`**: 失敗すると `into` に空の値を生成し、後続ステップは続行します。

出力が後続処理に必須なステップにはデフォルトの `fail` を使用してください。LLM が処理を続行できる付加的な補強には `skip` または `empty` を使用します。

## Phase が行ってはならないこと

[P8](principles.md#p8-phase-instructions-contain-only-domain-logic) に従い、Phase の指示は Preprocessor の動作を説明したり Control IR のメカニクスを列挙したりしてはなりません。指示は補強済みフィールドを名前（`data.stats`）で参照し、それをどう使うかを説明するにとどまります — それがどこから来たかを説明してはなりません。

## 関連情報

- [reference/dsl/preprocessor.md](../reference/dsl/preprocessor.md) — ステップの完全な構文とオプション。
- [concepts/postprocessor.md](postprocessor.md) — 対称的な Postprocessor（Skill 終了時に発火）。
- [concepts/principles.md](principles.md) — P3（OS が実行を制御）、P5（Workspace が唯一の信頼できる情報源）。
- [concepts/agent-engineering/system-design.md](agent-engineering/system-design.md)
  — システム設計原則としての決定論的分離。
