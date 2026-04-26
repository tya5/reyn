---
type: phase
name: judge
input: review_package
input_description: Review package containing the article and review result for the decision phase.
role: decision_maker
can_finish: true
---

Decide whether to finish the workflow or send the article back for revision.
Base your decision on data.review_result and finish_criteria.
Do NOT re-evaluate — only make the transition decision.

Convergence rules (apply in priority order):
1. score >= 0.8 → strongly prefer finish.
2. current_phase_visit >= max_phase_visit - 1 → strongly prefer finish.
3. Remaining issues are minor tweaks only → prefer finish.
4. Do NOT optimize indefinitely. Diminishing returns justify finishing.

Control IR format (ALL fields are required — output is rejected if any are missing):

If finishing:
  control.type = "finish"
  control.decision = "finish"
  control.next_phase = null
  artifact must match the "end" candidate's schema (title, body, quality_notes).
  Use data.article for title and body. Summarize the review outcome in quality_notes.

If revising:
  control.type = "transition"
  control.decision = "revise"
  control.next_phase = "revise"
  artifact must match the revise candidate's schema.
  Include: decision, reason, confidence, article (title+body carried forward), revision_notes (max 3 actionable items).

Always set:
  control.confidence (float 0.0–1.0) — certainty in this decision.
  control.reason = {"summary": "one-sentence rationale"} — MUST be an object, not a string.
