---
type: phase
name: review
input: draft_article | revised_article
input_description: Article (draft or revised) with title and body to be evaluated.
role: evaluator
---

Evaluate the article (title + body in data) against the finish_criteria.
Your ONLY job is evaluation — do NOT decide to finish or revise.
Carry the article forward unchanged so the next phase can act on it.

score MUST be a decimal between 0.0 and 1.0 (e.g. 0.8, not 4 or 5).
issues MUST contain at least 1 item. Even if the article is strong, identify at least one minor improvement — no article is perfect.
