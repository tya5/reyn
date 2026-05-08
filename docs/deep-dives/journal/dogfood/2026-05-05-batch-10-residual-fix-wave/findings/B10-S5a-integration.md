# B10-S5a Integration Retest — Natural Language eval_builder Routing

| Field | Value |
|---|---|
| Date | 2026-05-05 |
| main HEAD | `21c1497` |
| Verdict | **refuted** |
| B9 baseline | refuted ([B9-S5a-retest.md](../../2026-05-05-batch-9-fix-wave/findings/B9-S5a-retest.md)) |
| Predicted top (B10 prelude) | inconclusive (G16 wording fix was no-effect in B9) |
| B10 fixes active | B9-NEW-2 (`8f3bccf`) + indirect (B9-NEW-1/3 resolved) |

## Setup

- worktree: `agent-ab8bfc94972b0488f` (main HEAD `21c1497`)
- `.reyn/` flushed with `rm -rf` before run
- `reyn.local.yaml`: `permissions.python.trusted: allow` added temporarily (not committed)
- flag: `--allow-untrusted-python`
- trace dump: `REYN_LLM_TRACE_DUMP=.reyn/llm_trace_b10_s5a.jsonl`
- input: `direct_llm の eval を作って`
- 1 attempt (decisive — router responded immediately)
- Wall time: ~2s

## Observation

### Phase progression

No workflow started. Router responded with 1 LLM call, finish=stop, 0 tool calls.

```
Total LLM calls: 1
Skill workflows: 0
```

### Router response

Router replied with text (no tool call):

> "Skills are meant to be evaluated using the `eval` skill. The `eval` skill is a tool
> that allows you to evaluate other skills. To create an evaluation for the `direct_llm`
> skill, you would use the `eval_builder` skill to create an eval spec, and then use the
> `eval` skill to run the evaluation. Would you like me to help you create an eval spec
> for the `direct_llm` skill using `eval_builder`? If so, please provide me with the
> test cases you'd like to include."

The router correctly identified `eval_builder` as the right skill but chose to explain
and ask a clarifying question rather than invoke it. This is the G16 pattern —
router treats the natural-language request as ambiguous and requests confirmation.

### Tool calls

No `list_skills`, `describe_skill`, or `invoke_skill` calls. Single round-trip to LLM.

### Attractor detection

```
Total LLM calls: 1
Detected attractors: 0 (0%)
```

No attractor detected — the router chose a text reply directly without getting stuck.

### Cost

```
Total: $0.000269  |  2,336 tokens  |  1 LLM call
  gemini-2.5-flash-lite: $0.000269  2,336 tokens  (1 call)
```

## Delta vs batch 9

| Item | B9-S5a (330dd2a) | B10-S5a (21c1497) |
|---|---|---|
| Router action | text reply + clarification ask | text reply + clarification ask |
| eval_builder invoked | ❌ NO | ❌ NO |
| LLM calls | 1 | 1 |
| Tool calls | 0 | 0 |
| Attractor | 0 | 0 |
| Verdict | refuted | **refuted** |

No change from B9. The G16 wording fix and B9-NEW-2 fix did not affect this scenario.

## Verdict reasoning

**refuted**: The router did not invoke `eval_builder` for the natural-language request
`direct_llm の eval を作って`. It recognized the intent correctly (mentioned eval_builder
in its response) but chose to ask a clarifying question about test cases rather than
invoke the skill. This is an unresolved routing decision problem — the router's intent
classification did not produce an `Action` decision for this phrasing.

Note: This scenario tests G16 (natural-language routing to eval_builder). The G16 wording
fix (`330dd2a`) was documented as no-effect in B9. B10 confirms it remains unresolved.
The scenario is a different failure mode from S5b (which is a router attractor issue).

## Implications

### Root cause hypothesis

The router correctly identifies the skill (`eval_builder`) but its behaviour rule
("Action — run external work ... if user names a skill, use list_skills + invoke_skill")
requires an explicit skill name in the request. `eval を作って` does not name `eval_builder`
directly. The router's intent classification puts this in "Reply" (clarification) rather
than "Action".

Possible fix directions:
1. Add an example in eval_builder's skill description explicitly associating `eval を作って`
   with the skill
2. Modify the router behaviour rule to be more aggressive about Action intent when
   a skill can be inferred from the request object (`direct_llm` is mentioned)
3. Accept that single-turn natural-language routing is non-deterministic for ambiguous
   phrasings — document as a known UX limitation

### Batch 11 candidates

- G16 (natural-language eval_builder routing) — giveup-tracker entry still open
- Consider: router behaviour rule tuning for inferred-skill requests
