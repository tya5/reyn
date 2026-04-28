---
type: app
name: feedback_analyzer_app
entry: collect_feedback
final_output: app_improvement_plan
final_output_description: ユーザーフィードバックに基づいたアプリの改善提案。
finish_criteria:
  - ユーザーからのフィードバックが収集され、分析および改善提案が完了したとき。
  - アプリの目的が達成され、次のステップに進む準備ができたとき。
---

collect_feedback -> analyze_and_propose
