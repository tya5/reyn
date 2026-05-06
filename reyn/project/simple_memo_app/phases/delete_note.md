---
type: phase
name: delete_note
input: user_message
input_description: ユーザーからのメモ削除リクエスト。
role: deleter
model_class: standard
---

ユーザーからのリクエストを受け取り、指定されたメモを削除します。削除対象のメモは、ユーザーが指定したIDやタイトルに基づいて特定します。
