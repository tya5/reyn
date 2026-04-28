---
type: phase
name: judge
input: review_package
input_description: Review package containing the article and review result for the decision phase.
role: decision_maker
can_finish: true
---

Decide whether to finish the workflow or send the article back for revision.
Base your decision solely on data.review_result and finish_criteria.
Do NOT re-evaluate the article — only make the routing decision.

Convergence rules (apply in priority order):
1. score >= 0.8 → strongly prefer finish.
2. current_phase_visit >= constraints.max_phase_visits - 1 → strongly prefer finish.
3. Remaining issues are minor or stylistic only → prefer finish.
4. Do NOT optimize indefinitely. Diminishing returns justify finishing.

Choose the candidate that matches your decision:
- finish ("end" candidate): use data.article.title and data.article.body for the output article.
  Summarize the review outcome concisely.
- revise ("revise" candidate): carry data.article forward unchanged.
  Limit revision_notes to 3 actionable items.
