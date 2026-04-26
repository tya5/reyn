---
type: phase
name: review
input: draft_article | revised_article
input_description: Article (draft or revised) with title and body to be evaluated.
role: evaluator
---

Evaluate the article (title + body in data) against the finish_criteria.
Your ONLY job is evaluation — do NOT decide to finish or revise.
Carry the article forward so the next phase can act on it.

Set article to: {"title": "...", "body": "..."}
Set review_result to: {"strengths": [...], "issues": [...], "score": 0.0-1.0, "quality_notes": [...]}
