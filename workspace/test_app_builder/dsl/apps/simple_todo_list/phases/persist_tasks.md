---
type: phase
name: persist_tasks
input: task_list
input_description: 現在のTODOタスクのリスト。
role: data_persister
can_finish: true
---

現在のタスクリストを永続化します。アプリが再起動されてもタスクが失われないように、ローカルストレージやデータベースなどに保存する処理を実装してください。保存されたデータは、アプリ起動時に読み込めるようにします。
