---
type: phase
name: list_tasks
input: task_list
input_description: 現在登録されているTODOタスクのリスト。
role: task_lister
can_finish: false
---

与えられたタスクリストを、優先度順（高→中→低）に並べ替えて表示します。各タスクには、名前、詳細、優先度が表示されるようにしてください。ユーザーがタスクを一覧で確認できるUIを想定しています。
