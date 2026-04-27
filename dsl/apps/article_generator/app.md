---
type: app
name: article_generator
entry: generate_article
final_output: final_article
final_output_description: レビューを通過した最終的な記事。
finish_criteria:
  - 記事の生成が完了した。
  - レビューで承認された記事が出力された。
---

generate_article -> review_article
review_article -> generate_article