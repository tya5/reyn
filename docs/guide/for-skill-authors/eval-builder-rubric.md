---
type: agent
topic: stdlib
audience: [agent]
applies_to: [eval_builder]
---

# `eval_builder` — rubric construction rules

Use this when you (the `eval_builder` skill) generate an eval spec for a target skill. A good rubric is **specific, testable, and phase-keyed** — and it focuses on the things that actually matter to the skill's purpose.

## Spec shape

```markdown
---
skill_dsl_path: <target_skill_name>
model: standard
---

# Case: <case_name>

input: <input string or JSON artifact>

## Phase: <phase_name>
- <criterion 1>
- <criterion 2>

## Phase: <other_phase_name>
- <criterion>
```

One file = one or more cases. Each case has phase-keyed criteria.

## What makes a good criterion

A criterion must be:

1. **Specific.** "The summary is good" doesn't grade reliably. "The summary is 2-4 sentences" does.
2. **Phase-aligned.** Criteria target the phase that produces the relevant output. Don't put summary-quality criteria on the outline phase.
3. **Verifiable from output alone.** A judge with no access to the input shouldn't be able to give a wildly wrong score. If a criterion needs the input to evaluate, include enough of the input in the criterion text.
4. **Testable in both directions.** Criteria should fail on bad output. If you can't imagine an output that fails the criterion, it's not a useful test.
5. **Evidence-bound.** Criteria should require the output to *contain something specific* derived from the input, not just to *look right*. Shape-only criteria are gameable — an output that is structurally correct but content-empty can satisfy them.

   | Bad (shape only) | Good (evidence-bound) |
   |---|---|
   | "The paragraph is 2-4 sentences." | "The paragraph mentions all three angles from the bullet list." |
   | "The summary is well-structured." | "The summary names the specific topic from the input within its first sentence." |
   | "The response has at least two examples." | "Each example is drawn from the domains listed in the input constraints." |

   If a criterion can be satisfied by lorem ipsum that happens to have the right shape, rewrite it.

## Adversarial self-check

Before finalising a rubric, apply this check:

**Every eval spec SHOULD include at least one case whose criteria *fail* on a deliberately weak, empty, or off-topic output.** If you cannot imagine an output that fails your criteria, your criteria are gameable.

Concrete procedure:

1. Pick the most permissive criterion in each phase section.
2. Imagine submitting an output that is structurally correct but contains no real content (e.g. placeholder text, a bare `{}`, filler sentences).
3. If that imaginary output passes, the criterion is shape-only — add an evidence-bound clause or rewrite.

An eval spec with no failure-capable criteria offers no signal. A rubric that passes on empty input is indistinguishable from no rubric at all.

## Tags

| Tag | Effect |
|-----|--------|
| (default, no tag) | Required — counted in the pass threshold |
| `aspirational` | Optional — graded but doesn't fail the case |

Use `aspirational` for nice-to-haves that are genuinely subjective (tone, polish). Use the default for hard requirements (length, structure, factual presence).

## Cases

Cover at least:

- **Happy path** — typical input, all features exercised.
- **Edge case** — short/empty input, very long input, ambiguous input.
- **Anti-pattern** — input that should trigger a refusal / fallback (if the skill has one).

Three cases is a reasonable starting rubric. More if the skill has many code paths.

## Don't

- **Don't write criteria that only the eval skill knows.** A judge sees only the artifact and the criterion text — write criteria that make sense without extra context.
- **Don't restate phase logic.** The criteria test outcomes, not implementation. "The phase reads from memory" isn't a criterion; "the answer reflects the user's stated preference" is.
- **Don't grade things the runtime already validates.** Schema conformance is checked by the OS — don't add "the output has all required fields" as a criterion.
- **Don't use vague modifiers.** "Reasonable", "adequate", "good enough" — replace with concrete thresholds.

## Output expectations

When you finish:

1. Write `eval.md` to the target skill's directory.
2. Read it back, mentally run each criterion against an imaginary good output and an imaginary bad output. If both pass or both fail, rewrite.
3. Tell the user how to run it: `reyn eval <path/to/eval.md>`.
4. Note any pre-approvals the target skill needs (Python preprocessor steps, file write paths, etc.) — eval is non-interactive.

## Attribution

Adversarial-rubric discipline draws on Berkeley RDI's *Exploiting the most prominent AI agent benchmarks* (2026-04, [https://rdi.berkeley.edu/blog/trustworthy-benchmarks-cont/](https://rdi.berkeley.edu/blog/trustworthy-benchmarks-cont/)) which showed major benchmarks could be gamed with structurally-correct but content-empty submissions.

## See also

- [Reference: stdlib/eval](../../reference/stdlib/eval.md)
- [Reference: stdlib/eval_builder](../../reference/stdlib/eval_builder.md)
- [Tutorial: writing an eval](../getting-started/05-writing-an-eval.md)
