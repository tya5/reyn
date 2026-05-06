# B14-R1 Fix Verify — eval.run_target literal model string (B13-NEW-1)

| Field | Value |
|---|---|
| Date | 2026-05-06 |
| Batch | 14, R1 |
| Fix commit | (see git log) |
| Test suite | 1020 passed, 2 xfailed (+10 new tests) |

## Pre-fix reject reproduction

The bug was observed in B13-S4 session 3:
- `eval.run_target` LLM emitted `"model": "gpt-3.5-turbo"` in its `run_skill` op
- `run_skill.py` passed `"gpt-3.5-turbo"` to `invoke_sub_skill` unchanged
- `ModelResolver.resolve("gpt-3.5-turbo")` returned `"gpt-3.5-turbo"` (passthrough)
- LiteLLM proxy rejected: `BadRequestError: Invalid model name`
- LLM in `plan_improvements` chose `abort` after seeing the error

Full live re-reproduction not performed (would require dogfood session). Confirmed via code
trace at HEAD `2ea6302` — same passthrough behavior visible in `ModelResolver.resolve`.

## Post-fix behavior

After the fix:

1. `ModelResolver.is_known_class("gpt-3.5-turbo")` returns `False` (not in mapping).
2. `run_skill.handle()` detects `op.model="gpt-3.5-turbo"` is not a known class.
3. Falls back to `ctx.model="standard"` with a warning log message.
4. Sub-skill runs with `"standard"` → resolver maps to `"openai/gemini-2.5-flash-lite"` → proxy accepts.

## Changes

### `src/reyn/llm/model_resolver.py`

Added `is_known_class(name: str) -> bool`:
```python
def is_known_class(self, name: str) -> bool:
    """Return True if name is a configured model class (i.e. present in the mapping)."""
    return name in self._mapping
```

### `src/reyn/op_runtime/run_skill.py`

Added model class validation before using `op.model`:
```python
if op.model and ctx.resolver and not ctx.resolver.is_known_class(op.model):
    _log.warning(
        "run_skill: op.model %r is not a known model class — ignoring and "
        "inheriting runtime model %r instead. ...",
        op.model, ctx.model,
    )
    model = ctx.model or "standard"
else:
    model = op.model or ctx.model or "standard"
```

## Tests added

File: `tests/test_run_skill_model_class.py`

| Test | Tier | What it pins |
|---|---|---|
| `test_is_known_class_returns_true_for_configured_class` | 1 | `is_known_class` True for mapping entries |
| `test_is_known_class_returns_false_for_unknown_string` | 1 | `is_known_class` False for non-entries |
| `test_is_known_class_false_for_empty_mapping` | 1 | empty mapping — nothing is known |
| `test_resolve_still_passes_through_unknown` | 1 | backward compat passthrough preserved |
| `test_run_skill_uses_known_class_from_op` | 2b | known class → used as-is |
| `test_run_skill_falls_back_when_op_model_is_literal` | 2b | `gpt-3.5-turbo` → fallback to ctx.model (B13-NEW-1 scenario) |
| `test_run_skill_falls_back_when_op_model_is_gpt4` | 2b | `openai/gpt-4o` → fallback |
| `test_run_skill_uses_ctx_model_when_op_model_empty` | 2b | empty op.model → ctx.model |
| `test_run_skill_defaults_to_standard_when_both_empty` | 2b | both empty → "standard" |
| `test_run_skill_op_schema_model_field_defaults_empty` | 1 | `RunSkillIROp.model` default="" |

Total: 10 tests (4 Tier 1 + 6 Tier 2b)

## Test suite result

```
1020 passed, 2 xfailed  (+10 new tests; was 1010 passed before fix)
```

No regressions.

## Step 2 retest plan

For the N=5 stability retest (batch 14 step 2), the expected behavior change:
- Session 3 B13-S4-style abort should no longer occur (or at least not due to `gpt-3.5-turbo`)
- If LLM hallucinates `gpt-3.5-turbo`, it is now silently replaced with `ctx.model`
- Expected: ≥4/5 complete rate (same as B13-S4 baseline, or improvement)

The retest will confirm whether the 20% partial rate from B13-S4 is eliminated by this fix.
