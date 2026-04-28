---
type: app
name: simple_todo_list
entry: create_task
final_output: app_implementation
final_output_description: 構築されたTODOリスト管理アプリのコード。
finish_criteria:
  - タスクの追加、編集、削除、優先度変更の機能が実装されていること。
  - ユーザーインターフェースが直感的で使いやすいこと。
  - TODOリストが永続化され、アプリ再起動後も保持されること。
---

create_task -> list_tasks
create_task -> persist_tasks
list_tasks -> create_task
list_tasks -> update_task
list_tasks -> delete_task
list_tasks -> persist_tasks
update_task -> list_tasks
update_task -> persist_tasks
delete_task -> list_tasks
delete_task -> persist_tasks
persist_tasks -> finish
