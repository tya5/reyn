---
type: phase
name: create_note
input: user_message
input_description: ユーザーからのメモ作成リクエスト。
role: writer
model_class: standard
---

ユーザーからの入力を受け取り、新しいメモを作成します。メモの内容はユーザーメッセージから抽出します。メモはデータベースまたはファイルに保存されることを想定します。
