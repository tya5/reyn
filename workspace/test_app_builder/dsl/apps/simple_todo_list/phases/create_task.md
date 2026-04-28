---
type: phase
name: create_task
input: user_message
input_description: ユーザーからのタスク追加や管理に関する指示。
role: task_creator
can_finish: false
---

ユーザーの指示に基づき、新しいTODOタスクを作成または既存タスクを更新します。タスク名、詳細、優先度（高・中・低など）を設定できるようにします。ユーザーからの入力を解析し、必要な情報を抽出してタスクオブジェクトを生成してください。
