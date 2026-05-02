---
type: how-to
topic: runtime
audience: [human]
applies_to: [phases/*.md]
---

# Phase の途中でユーザーに質問する

**目的:** Phase を一時停止し、ユーザーに質問し、回答を入力にマージした上で同じ Phase を再開する。

## 使うべき状況

- Phase が情報のほぼすべてを持っており、欠けている情報が推測できない。
- 推測が外れた場合のコストが、1 回の追加プロンプトのコストより高い。

## パターン

Phase の指示で「いつ」質問するかを記述します。LLM は `ask_user` Control IR op を出力します:

```
もし `relevant_memories` にユーザーが好む出力言語が指定されていない場合、
続ける前にユーザーに確認してください。
```

LLM が出力する op:

```json
{
  "kind": "ask_user",
  "question": "どの言語で投稿を書くべきですか？",
  "suggestions": ["English", "Japanese"]
}
```

## OS が行うこと

1. 質問（と suggestions がある場合はそれも）を表示します。
2. レスポンスが来るまで stdin を読みます。
3. 元の入力と Q&A を `user_message` artifact にマージします。
4. マージされた artifact で**同じ Phase** を再実行します。訪問カウントは増加しません。

2 つのイベントが発行されます: `user_intervention_requested` と `user_intervention_received`。

## Phase の指示: 言うべきこと / 言わないこと

**言うべきこと:**

- いつ質問するか（トリガー条件）。
- 何を質問するか（ドメイン固有の質問）。

**言わないこと:**

- op の JSON 形式（`{kind: ask_user, question: ...}`）。OS がこれを `available_control_ops` に注入します（P8）。

## 注意事項

- `reyn eval` は非インタラクティブです。Phase の途中でユーザーに質問する Skill は eval モードでハングします。eval では決して真にならない条件で質問をゲートするか、eval スペックで欠けているフィールドを事前に提供してください。
- 1 つの op で複数の質問をしないでください。一度に 1 つの `ask_user` op を使用してください。さらに質問が必要な場合は、1 つ聞いて回答を得てから（これで同じ Phase に再入します）、次の質問が必要かどうかを判断してください。

## 関連情報

- [リファレンス: control-ir](../reference/runtime/control-ir.md) — `ask_user`
- [リファレンス: events](../reference/runtime/events.md) — `user_intervention_*` イベント
- [コンセプト: principles P8](../concepts/principles.md)
