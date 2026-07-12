---
name: draft_judge_revise
description: Draft an artifact, score it with judge_output against a rubric, and revise on failure -- the standard Evaluation-gated workflow for any "produce then check quality" task (a summary, a doc section, an email, a report paragraph). Read this before handing off a self-authored artifact you have not gated.
---

# Draft -> judge -> revise

The first-hour common task this skill covers: you produce a piece of text
(a draft), and before you hand it off or ship it you want an objective pass
against a rubric rather than your own say-so. This is the CLAUDE.md
Evaluation lens applied to a single artifact, not just to a promoted
skill/pipeline/hook (0060 Addendum D9.5 curated-5 #3).

## The loop

1. **Draft.** Produce the artifact as you normally would (write the summary,
   the section, the email).
2. **Judge.** Call `judge_output(data_inline=<the draft>, rubric="...",
   threshold=<0.0-1.0>)`. Write the rubric yourself, in plain language --
   the OS never interprets it (P7); it just scores your draft against your
   own criteria and returns `{score, passed, reason}`.
3. **Revise on fail.** If `passed` is `false`, revise the draft using
   `reason` as the note to fix, then re-run `judge_output` on the revised
   draft. **Bound the loop** (e.g. stop after 2 revisions and hand off the
   best attempt with a caveat) rather than looping indefinitely --
   Reliability lens: bounded loops with graceful force-close, never an
   unbounded retry.
4. **Hand off.** Once the draft passes (or the revision budget is spent),
   show it to the operator via `present` (see `reyn_cheat_sheet`) instead of
   pasting it into your reply, or continue with whatever workflow requested
   the draft.

**Worked example** -- a `judge_output` call scoring a drafted paragraph
(this exact JSON is CI-verified as a valid `judge_output` argument set
against the real op schema):

```json
{
  "data_inline": "Reyn is an agent OS: it makes every LLM-driven action typed, permissioned, audited, and recoverable by construction.",
  "rubric": "Score 0.0-1.0: is this paragraph clear, accurate, and free of jargon for a first-time reader? Reply as JSON {\"score\": <0-1 float>, \"reason\": <string>}.",
  "threshold": 0.8,
  "on_fail": "continue"
}
```

If the call above returned `passed=false`, the next step is: revise the
draft using the returned `reason`, then re-issue the same `judge_output`
call with `data_inline` set to the revised text -- not a different rubric,
not a lowered threshold (that would be gaming the gate, not passing it).

Full `judge_output` spec: `docs/reference/runtime/control-ir.md` (`judge_output`
section).
