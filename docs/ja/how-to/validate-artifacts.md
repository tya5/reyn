---
type: how-to
topic: dsl
audience: [human]
applies_to: [phases/*.md]
---

# LLM が受け取る前に artifact を検証する

**目的:** LLM が呼び出される前に不正な入力をキャッチし、LLM が反応できるように（ユーザーへの質問、拒否、フォールバック）所見を表面化する。

## 使うべき状況

- 入力 artifact に LLM が検証するにはコストがかかる構造がある（長いリスト、ネストされたオブジェクト、クロスフィールド制約を持つオプションフィールド）。
- 「LLM がおそらく気付く」ではなく、決定論的なゲートが欲しい。

## パターン

Phase の preprocessor に `validate` ステップを追加します。所見は `into` に格納され、LLM はそれを他の入力フィールドと同様に読み取ります。

```yaml
---
type: phase
name: triage
input: ticket_batch
preprocessor:
  - validate:
      schema:
        type: object
        required: [tickets]
        properties:
          tickets:
            type: array
            items:
              type: object
              required: [id, title]
              properties:
                id: { type: string }
                title: { type: string, minLength: 1 }
      target: input
      into: validation_findings
---

各チケットを `bug`、`feature`、`chore` にトリアージしてください。
`validation_findings.errors` が空でない場合は、トリアージの前に
ユーザーに入力を修正するよう依頼してください。
```

## `validation_findings` の形式

```json
{
  "errors":   [{"path": "tickets[3].title", "message": "must NOT be shorter than 1 chars"}],
  "warnings": [],
  "valid":    false
}
```

LLM は `into` で設定したキーの下でそれを読み取ります。Phase の指示はスキーマを知らずにフィールド名で所見を参照します（P8）。

## 検証が自動で行われる箇所

以下のいずれかには `validate` ステップは不要です。OS が行います:

- **トランジション検証。** すべての artifact はトランジション前に次の Phase の `input` スキーマに対して検証されます。
- **最終出力の検証。** 終了 artifact は Skill の `final_output_schema` に対して検証されます。

`validate` は LLM 呼び出しの前、Phase の**内部**で検証したい場合にのみ使用します。

## 関連情報

- [リファレンス: preprocessor](../reference/dsl/preprocessor.md) — `validate` ステップ
- [リファレンス: artifact.yaml](../reference/dsl/artifact-yaml.md)
- [compose-skills-with-run-skill.md](compose-skills-with-run-skill.md)
