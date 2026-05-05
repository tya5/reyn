# B11-R1: _resolved_paths schema gap (B10-NEW-1 root cause)

**Bug label:** B10-NEW-1  
**Fix wave:** Batch 11 R1  
**Date:** 2026-05-05  
**Status:** Fixed

## Symptom

During dogfood S1 (skill_improver on direct_llm), `eval.run_target` failed with
`run_skill` ops referencing non-existent paths. Two different invalid path forms
were observed across successive iterations:

- `/tmp/reyn-workspace/skills/direct_llm` (hyphen)
- `/tmp/reyn_workspace/direct_llm` (underscore)

The inconsistency between iterations was labeled "temp workspace path mismatch"
and initially hypothesized to be a string typo somewhere in the OS code.

## Root cause

The true root cause was not a typo — it was a schema gap that caused a loss
of OS-resolved path information.

### Data flow (pre-fix)

1. `copy_to_work` preprocessor runs `inject_resolved_paths` and writes
   `_resolved_paths` into `input_artifact.data` (alongside `validation`).
2. The LLM is shown this data in the `ContextFrame` and asked to emit an
   `improvement_session` artifact.
3. The LLM emits the artifact — but `_resolved_paths` was absent from the
   `improvement_session` schema `properties`.
4. `_strip_data` (in `artifact_validator.py`) removes any field not in
   `schema.properties`. Since `_resolved_paths` was not declared, it was
   silently stripped with a "removed unknown field" correction.
5. Downstream phases (`run_and_eval`, `plan_improvements`, `apply_improvements`,
   `finalize`) received the session **without any path information**.
6. `run_and_eval` instructed the LLM to use `session._resolved_paths.target_skill_path`
   when building `eval_case_input`, but the field was absent. The LLM hallucinated
   path strings — producing different invalid paths on each run.

### Why hyphen vs underscore

The path strings observed (`/tmp/reyn-workspace/...` and `/tmp/reyn_workspace/...`)
were both hallucinations. Python package names use underscores; CLI conventions
sometimes use hyphens. The LLM was essentially guessing based on prior training
data, producing different values on each invocation.

### The `_strip_data` mechanism

```python
def _strip_data(data: dict, schema: dict, corrections: list[str], *, _top_level: bool = True) -> dict:
    props = schema.get("properties", {})
    result: dict[str, Any] = {}
    for key, value in data.items():
        if key == "type" and _top_level:
            corrections.append("removed 'type' from data (injected by LLM)")
            continue
        if props and key not in props:            # ← _resolved_paths hit this
            corrections.append(f"removed unknown field '{key}'")
            continue
        ...
```

Any field not in `schema.properties` is silently removed. Before the fix,
`_resolved_paths` was not in `improvement_session.yaml`'s `properties` block,
so it was always removed.

## Fix

Two changes, applied together:

### 1. Schema fix — `improvement_session.yaml`

Added `_resolved_paths` to the `properties` block (with full sub-schema for all
four path fields) and to the top-level `required` list. This makes `_strip_data`
preserve the field and makes the OS reject any LLM output that omits it.

### 2. Instruction fix — `copy_to_work.md`

Added a `CRITICAL` carry-through instruction:

> The emitted `improvement_session` artifact MUST include `_resolved_paths`
> copied exactly from `data._resolved_paths`. Do NOT construct path strings
> yourself. Do NOT omit this field — downstream phases all depend on these
> OS-resolved paths.

This ensures even weak LLMs understand they must carry the field verbatim.

### Fixture update

Changing `copy_to_work.md` instructions changes the `ContextFrame.instructions`
field, which changes the SHA-256 replay fixture key. The two affected fixtures
were updated with recomputed keys and responses that include `_resolved_paths`
in the emitted artifact:

- `tests/fixtures/llm/copy_to_work_validation/validation_ok.jsonl`
  - old key: `2abd95a1...`
  - new key: `1987c2fe...`
- `tests/fixtures/llm/copy_to_work_validation/validation_fail.jsonl`
  - old key: `af4421df...`
  - new key: `8cb08f2c...`

## Tests added

New file: `tests/test_improvement_session_schema.py` (Tier 2, 5 tests)

- `test_improvement_session_schema_declares_resolved_paths` — schema has `_resolved_paths` in properties
- `test_improvement_session_schema_resolved_paths_is_required` — it is in the required list
- `test_improvement_session_schema_resolved_paths_sub_properties` — all four sub-fields declared
- `test_strip_data_preserves_resolved_paths_when_declared` — `_strip_data` keeps it (post-fix behavior)
- `test_strip_data_removes_resolved_paths_when_not_in_schema` — counter-test pinning pre-fix behavior

## Lessons

- **Schema gaps silently break data flow.** `_strip_data` is a correctness guard
  but only for fields declared in the schema. Fields not declared are invisible to
  the guard and will be stripped.
- **Determinism vs non-determinism split.** The preprocessor deterministically
  computes paths; the LLM must carry them through verbatim. The schema is the
  contract that makes this carrythrough enforceable.
- **Path names in LLM output are always suspect.** If the LLM generates any
  path string without a clear OS-derived source, it is a hallucination. The
  only safe approach is: OS resolves → LLM copies verbatim → schema enforces.
