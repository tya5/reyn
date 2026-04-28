---
type: phase
name: delete_task
input: task_delete_request
input_description: ユーザーからのタスク削除のリクエスト。
role: task_deleter
can_finish: false
---

ユーザーからのリクエストに基づき、指定されたタスクをタスクリストから削除します。削除対象のタスクを特定し、リストから安全に削除してください。
