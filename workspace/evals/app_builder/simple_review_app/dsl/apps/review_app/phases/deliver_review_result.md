---
type: phase
name: deliver_review_result
input: review_outcome
input_description: レビューの結果（承認または却下、および却下理由）。
role: delivery_agent
can_finish: true
---

レビューの結果をユーザーに通知します。承認された場合はその旨を、却下された場合は却下理由とともに通知してください。
