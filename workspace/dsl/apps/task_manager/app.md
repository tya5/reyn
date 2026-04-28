---
type: app
name: task_manager
entry: input_task
final_output: delivered_task_output
final_output_description: ユーザーに提示される、優先度付けと要約が完了したタスクの詳細。
finish_criteria:
  - タスクが正常に入力され、優先度付けと要約が行われたとき。
  - ユーザーがアプリの利用を終了したとき。
---

input_task -> prioritize_and_summarize_task
prioritize_and_summarize_task -> deliver_task_details
