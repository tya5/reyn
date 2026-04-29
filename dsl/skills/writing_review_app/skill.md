---
type: skill
name: writing_review_app
entry: analyze
final_output: final_article
final_output_description: |
  Final article ready to return to the user.
  Must include title, body, and quality_notes summarizing the review outcome.
finish_criteria:
  - audience_fit
  - clarity
  - specificity
  - structure
  - language_consistency
max_phase_visits:
  review: 3
  judge: 3
  revise: 3
---

analyze -> draft -> review -> judge
judge -> revise -> review
