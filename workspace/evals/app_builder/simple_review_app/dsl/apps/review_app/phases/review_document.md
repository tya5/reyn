---
type: phase
name: review_document
input: submitted_document
input_description: 提出されたドキュメント。
role: reviewer
can_finish: false
---

提出されたドキュメントをレビューします。承認する場合は `approved` を `true` に設定し、差し戻しの場合は `approved` を `false` に設定して `feedback` に理由を記述します。レビュー結果は `review_result` アーティファクトに保存されます。
