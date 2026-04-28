---
type: phase
name: prioritize_and_summarize_task
input: task_input
input_description: ユーザーによって入力されたタスク。
role: task_processor
can_finish: false
---

入力されたタスクを分析し、優先度を決定する。また、タスクの内容を簡潔に要約する。優先度と要約結果を次のフェーズに渡す。
