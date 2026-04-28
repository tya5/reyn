---
type: app
name: article_generator
entry: generate_article
final_output: delivered_article
final_output_description: レビューを通過した最終的な記事。
finish_criteria:
  - 記事が生成され、レビューで承認された。
  - レビューで修正が必要と判断され、再度生成・レビューが行われた。
---

generate_article -> review_article
review_article -> generate_article
review_article -> deliver_article
