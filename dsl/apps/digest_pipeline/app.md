---
type: app
name: digest_pipeline
entry: prepare
final_output: digest_result
final_output_description: |
  Digest of the generated article: a concise summary and key points
  extracted after the full writing and review cycle.
---

prepare -> @writing_review_app -> digest
