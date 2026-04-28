---
type: app
name: feedback_analyzer
entry: collect_feedback
final_output: app_output
final_output_description: アプリの最終的な実行結果。
finish_criteria:
  - ユーザーからのフィードバックが収集された。
  - 収集されたフィードバックが分析され、具体的な改善提案が生成された。
---

collect_feedback -> analyze_and_propose
