# eval-a-skill

Score an existing skill against a single test case using the `eval` stdlib
skill (LLM-as-judge under the hood via `judge_phase`).

## What this shows

- Running `eval` on a target skill.
- The shape of an eval test case: `target`, `input`, optional `criteria`.
- Where the verdict, score, and weakest-phase fields land.

## Run it

The `eval` skill takes a description of the target and the test case as
its initial input. The cleanest way is JSON:

```bash
reyn run eval '{
  "type": "eval_request",
  "data": {
    "target_skill": "word_stats_demo",
    "test_input": {"type": "user_message", "data": {"text": "The quick brown fox jumps over the lazy dog."}},
    "criteria": [
      "Output reports a non-zero word_count",
      "Output reports a non-zero char_count",
      "No phase errored"
    ]
  }
}'
```

`word_stats_demo` is bundled stdlib, so this works out of the box from the
repo root.

## Expected output

A `final_output` of type `eval_result`:

```json
{
  "verdict": "pass",
  "overall_score": 1.0,
  "per_criterion": [
    {"criterion": "Output reports a non-zero word_count", "met": true},
    {"criterion": "Output reports a non-zero char_count", "met": true},
    {"criterion": "No phase errored", "met": true}
  ],
  "weakest_phase": null
}
```

## Variations

- Point at one of your own skills under `reyn/local/<name>/` by changing
  `target_skill`.
- Drop `criteria` to let the judge generate ad-hoc criteria from the task
  description.
- `--events` to see the underlying `judge_phase` invocations.

## See also

- [stdlib/eval](https://github.com/tya5/reyn/blob/main/src/reyn/stdlib/skills/eval/skill.md)
- [stdlib/judge_phase](https://github.com/tya5/reyn/blob/main/src/reyn/stdlib/skills/judge_phase/skill.md)
- [Tutorial: writing an eval](../../guide/getting-started/05-writing-an-eval.md)
- Next: [improve-a-skill](../improve-a-skill/README.md) — feed eval results into
  `skill_improver` to actually raise the score.
