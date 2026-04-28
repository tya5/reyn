---
type: phase
name: review_draft
input: draft_article
input_description: 生成された記事のドラフト。
role: reviewer
can_finish: true
---

記事のドラフトをレビューしてください。具体的には、内容の一貫性、論理性、誤字脱字がないかを確認してください。承認する場合は'approved'をtrueに、修正が必要な場合は'approved'をfalseにし、'feedback'に具体的な修正点を記述してください。
