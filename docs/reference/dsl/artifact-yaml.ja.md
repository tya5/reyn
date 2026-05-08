---
type: reference
topic: dsl
audience: [human, agent]
applies_to: [artifacts/*.yaml]
---

# `artifacts/<name>.yaml`

artifact は Phase 間で受け渡される型付きの構造化された値です。各 artifact は Skill の `artifacts/` ディレクトリに YAML スキーマを持ちます。

## 最小限の例

```yaml
# artifacts/topic_input.yaml
type: object
required: [topic]
properties:
  topic:
    type: string
    description: Subject the skill should write about.
```

## スキーマ

Reyn の artifact ファイルは YAML で表現された JSON Schema（Draft-7）フラグメントです。

| フィールド | 必須 | 備考 |
|-------|----------|-------|
| `type` | yes | ほぼ常に `object`。 |
| `required` | 省略可能 | 必須プロパティ名のリスト。 |
| `properties` | yes（オブジェクトの場合） | 名前 → JSON Schema のマップ。 |
| `description` | 省略可能 | 自由形式の説明；LLM コンテキストに表示されます。 |
| `additionalProperties` | 省略可能 | デフォルト: `true`。厳格な形状には `false` に設定します。 |

## 厳格vs寛容なバリデーション

デフォルトでは、Reyn はトップレベルのみを検証します。ネストされた必須フィールドは強制されません。すべてのネスト深さで必須フィールドを強制するには `--strict` を渡します。

## クロス Skill artifact（stdlib）

`src/stdlib/artifacts/*.yaml` 配下の artifact はすべての Skill で利用できます。最も一般的なのは `user_message` です:

```yaml
# src/stdlib/artifacts/user_message.yaml
type: object
required: [text]
properties:
  text:
    type: string
    description: Free-text user input.
```

自然言語入力を受け入れる Skill は、エントリー Phase で `input: user_message | <other_artifact>` と宣言します。

## 命名規約

- ファイル: `lowercase_snake_case.yaml`。
- 型: `.yaml` を除いたファイル名が artifact の型名。
- プロパティ: `lowercase_snake_case`。
- 何でも入れる artifact を避ける。あるプロパティが 1 つの Phase にのみ必要な場合は、その Phase の入力 artifact に入れます。

## ランタイムでのバリデーション

- トランジション時: LLM の `artifact.data` が次の Phase の入力スキーマに対して検証されます。
- 完了時: Skill の `final_output_schema` に対して検証されます。
- 失敗すると `validation_error` イベントが発行され、再プロンプトされます（リトライ制限に従います）。

## 関連情報

- [skill-md.md](skill-md.md) — `final_output` は artifact 名を参照する
- [phase-md.md](phase-md.md) — Phase の `input` は 1 つ以上の artifact 名を参照する
- [graph.md](graph.md) — グラフ + artifact の解決
