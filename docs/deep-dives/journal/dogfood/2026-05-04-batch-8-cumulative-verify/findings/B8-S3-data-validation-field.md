# B8-S3 data.validation Field — RETRO-H3 Unblock Final Verify

| Field | Value |
|---|---|
| Date | 2026-05-05 |
| main HEAD | `8e15019` |
| Verdict | **blocked** |
| Predicted top | blocked (40%) / verified (30%) |

## Setup

Observed within the same single session as S1 (input wording identical).
Target phase: `copy_to_work` preprocessor execution. LLM fallback path observation.

## Observation

### Phase reach status

```
analyze_skill (eval_builder, inside prepare): REACHED — 2 preprocessor steps, 5 LLM turns
copy_to_work: NOT REACHED
```

### analyze_skill preprocessor execution

```
preprocessor_step_started  step_index=0  step_type=python  (analyze_skill_resolver.py: compute_paths)
python_step_completed       step_index=0  module=./analyze_skill_resolver.py  function=compute_paths
preprocessor_step_started  step_index=1  step_type=python  (analyze_skill.py: inject_resolved_paths)
python_step_completed       step_index=1  module=./analyze_skill.py  function=inject_resolved_paths
```

Both preprocessor steps for `analyze_skill` completed successfully. This means:
- B8-NEW-2 (PureModeViolation fix) is confirmed working: `analyze_skill_resolver.py` no longer
  fails with `from reyn.skill.skill_paths import ...` violation.
- The LLM then ran 5 act turns (file read/glob attempts), all blocked by permission_denied.

### copy_to_work preprocessor: not reached

`copy_to_work` was never started. The chain aborted inside `analyze_skill` (eval_builder
sub-skill) before `prepare` could transition to `copy_to_work`.

### H3 hypothesis: data.validation transparency

B6-S1-M1 hypothesis (a): when `copy_to_work` LLM runs, the input artifact's `data.validation`
field is transparently referenced by the LLM as part of the `participant` field resolution.

To observe this, `copy_to_work` must:
1. Be reached (requires `prepare` + `analyze_skill` to complete successfully)
2. Fall back to LLM path (preprocessor must fail or be absent)

In this session:
- Step 1 failed: `copy_to_work` was not reached
- Step 2 is moot

### artifact_created events

```
artifact_created: phase=_input  keys=[max_improvement_rounds, description, target_skill, spec_...]
artifact_created: phase=_input  type=eval_builder_request  keys=[target_skill]
artifact_created: phase=analyze_skill_preprocessed  type=eval_builder_request  keys=[target_skill, _prep, _res...]
```

The `analyze_skill_preprocessed` artifact was created (preprocessor completed), but the workflow
aborted before this artifact's content could be used for further phases. There is no `data.validation`
field observable in this chain position.

## Verdict reasoning

`blocked`: Predicted as the top outcome (40%). `copy_to_work` was never reached, so the
`data.validation` field cannot be observed in LLM context. The blocker is the same B8-NEW-1
permission issue that prevented `analyze_skill` from completing.

The H3 hypothesis (whether LLM in `copy_to_work` transparently references `data.validation`)
remains **unobserved**. The preprocessor path for `copy_to_work` would bypass the H3 concern
entirely (preprocessor uses deterministic `compute_paths`, not LLM field selection).

Therefore H3 is in a superposition state: either the preprocessor route makes it moot
(deterministic bypass), or if fallback is ever triggered, H3 could still manifest. This
cannot be resolved without reaching `copy_to_work` in an LLM fallback path.

Prediction was 30% verified, 40% blocked. Actual: blocked. Top prediction was correct.

## Implications

- H3 verification requires unblocking the chain through `analyze_skill` + `prepare` completion.
- Once B8-NEW-1 is fixed, chain may still hit `copy_to_work` preprocessor-only path —
  in which case H3 observation requires deliberately triggering LLM fallback in `copy_to_work`
  (e.g., by temporarily removing or breaking the preprocessor).
- Consider adding a specific batch 9 scenario for H3: run `copy_to_work` in isolation via
  `--start-phase copy_to_work` if such a flag exists, or construct a synthetic input artifact.
- The `analyze_skill` preprocessor completing successfully (B8-NEW-2 fix confirmed) is a
  secondary positive signal: the anyOf union input path is working up to the permission wall.
