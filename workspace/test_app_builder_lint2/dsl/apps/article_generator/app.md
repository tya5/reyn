---
type: app
name: article_generator
entry: generate_draft
final_output: final_article
final_output_description: レビューが承認された最終的な記事。
finish_criteria:
  - 記事のドラフトが生成された。
  - 生成された記事がレビューで承認された。
---

generate_draft -> review_draft
review_draft -> generate_draft
review_draft -> finish
