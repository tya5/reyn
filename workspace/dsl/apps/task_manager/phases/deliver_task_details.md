---
type: phase
name: deliver_task_details
input: processed_task
input_description: 優先度付けと要約が行われたタスク。
role: output_formatter
can_finish: true
---

優先度と要約されたタスクの詳細をユーザーに分かりやすく提示する。このフェーズが完了すると、アプリの機能は終了する。
