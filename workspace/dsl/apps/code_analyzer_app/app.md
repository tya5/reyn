---
type: app
name: code_analyzer_app
entry: analyze_code
final_output: final_explanation_article
final_output_description: 最終承認されたアーキテクチャ解説記事。
finish_criteria:
  - アーキテクチャ解説記事が完成し、最終レビューで承認された場合。
  - レビューとリバイスの最大試行回数に達した場合。
---

analyze_code -> generate_explanation
generate_explanation -> review_explanation
review_explanation -> revise_explanation
review_explanation -> deliver_article
revise_explanation -> review_explanation
