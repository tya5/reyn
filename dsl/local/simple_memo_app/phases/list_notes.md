---
type: phase
name: list_notes
input: user_message
input_description: ユーザーからのメモ一覧表示リクエスト。
role: reader
model_class: standard
---

ユーザーからのリクエストを受け取り、保存されている全てのメモを一覧表示します。各メモのタイトルまたは内容の冒頭を表示します。
