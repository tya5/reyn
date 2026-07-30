---
name: draft_judge_revise
description: Draft an artifact, self-review it against your own checklist via a schema-validated agent step, and revise on failure -- the standard Evaluation-gated workflow for any "produce then check quality" task (a summary, a doc section, an email, a report paragraph). Read this before handing off a self-authored artifact you have not gated.
---

# Draft -> self-review -> revise

The first-hour common task this skill covers: you produce a piece of text
(a draft), and before you hand it off or ship it you want a structured pass
against a checklist rather than shipping it unexamined. This is the CLAUDE.md
Evaluation lens applied to a single artifact, not just to a promoted
skill/pipeline/hook (0060 Addendum D9.5 curated-5 #3).

**Be honest about what this is.** It is **self-review, not objectivity** --
you write the draft, you write the checklist, and the same model family
scores it. That is useful for catching requirements your own checklist names
and your draft missed (a real, valuable check), but it is not an independent
judge. Do not oversell it as one.

## The loop

1. **Draft.** Produce the artifact as you normally would (write the summary,
   the section, the email).
2. **Self-review via a schema-validated agent step.** Run a small pipeline
   (via `run_pipeline_inline`, see the worked example below) whose one step
   is an `agent:` step with `schema:` set to a small schema you declare
   (e.g. `{score: number, reason: string}`). The `schema:` **constrains the
   agent's generation and validates the parsed result** -- the OS's actual
   contribution here (typed, validated output), not a bespoke scorer op.
3. **Revise on fail.** Compare `ctx.verdict.score` against your own threshold
   (a plain `transform` step, or a check in your own reasoning) and, if it
   falls short, revise the draft using `ctx.verdict.reason` as the note to
   fix, then re-run the same self-review step on the revised draft. **Bound
   the loop** (e.g. stop after 2 revisions and hand off the best attempt with
   a caveat) rather than looping indefinitely -- Reliability lens: bounded
   loops with graceful force-close, never an unbounded retry.
4. **Hand off.** Once the draft passes (or the revision budget is spent),
   show it to the operator via `present` (see `reyn_cheat_sheet`) instead of
   pasting it into your reply, or continue with whatever workflow requested
   the draft.

**Worked example** -- a self-review pipeline definition scoring a drafted
paragraph (this exact YAML is CI-verified: it parses via the real pipeline
parser and passes the real `run_pipeline_inline` static-analysis gate --
schema ref resolves, no nested launch, agent-step identity unset):

```yaml
pipeline: self_review
steps:
  - agent:
      prompt: >-
        Self-review this draft against your own checklist: is it clear,
        accurate, and free of jargon for a first-time reader? Draft:
        {ctx.draft}. Give a score in [0.0, 1.0] and a short reason.
      schema: Verdict
      output: verdict
---
schema: Verdict
fields:
  score: {type: number}
  reason: {type: string}
```

Launch it with:

```
run_pipeline_inline(
  definition="<the two documents above>",
  input={draft: "Reyn is an agent OS: it makes every LLM-driven action typed, permissioned, audited, and recoverable by construction."},
)
```

If `ctx.verdict.score` comes back below your threshold, the next step is:
revise the draft using `ctx.verdict.reason`, then re-issue the same
self-review pipeline with `input.draft` set to the revised text -- not a
different checklist, not a lowered threshold (that would be gaming the gate,
not passing it).

Full `agent` step + `schema` spec: `docs/reference/runtime/pipeline-dsl.md`
(`AgentStep` / schema sections); the Evaluation lens rationale:
`docs/concepts/agent-engineering/evaluation.md`.
