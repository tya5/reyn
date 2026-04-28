---
type: phase
name: submit_document
input: user_message
input_description: ユーザーからのドキュメント提出リクエスト。
role: user
can_finish: false
---

ユーザーからのドキュメント提出を受け付け、レビュー対象として記録します。提出されたドキュメントは `submitted_document` アーティファクトに保存されます。
