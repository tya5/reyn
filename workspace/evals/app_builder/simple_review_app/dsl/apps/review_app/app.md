---
type: app
name: review_app
entry: submit_document
final_output: app_completion_report
final_output_description: アプリのビルド完了レポート。
finish_criteria:
  - ドキュメントが承認された場合
  - ドキュメントが最終的に差し戻された場合
---

submit_document -> review_document
review_document -> deliver_result
