---
type: phase
name: review_article
input: article_draft
input_description: 生成された記事の下書き。
role: reviewer
can_finish: true
---

生成された記事の下書きを、明確さ、構造、正確さ、文法、スタイルに関してレビューしてください。承認するか、修正が必要な点を具体的に指摘してください。承認された場合は approved: true、修正が必要な場合は approved: false と feedback フィールドに具体的なフィードバックを記述してください。
