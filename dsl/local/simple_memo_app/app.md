---
type: app
name: simple_memo_app
description: シンプルなメモの作成、表示、削除ができるメモアプリ。
entry: create_note
final_output: built_app
final_output_description: 構築されたアプリケーション。
finish_criteria:
  - メモの作成、表示、削除が正常に完了する。
  - ユーザーインターフェースがシンプルで直感的に操作できる。
---

create_note -> list_notes
create_note -> delete_note
create_note -> deliver_app
list_notes -> create_note
list_notes -> delete_note
list_notes -> deliver_app
delete_note -> create_note
delete_note -> list_notes
delete_note -> deliver_app
