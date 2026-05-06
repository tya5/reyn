# B14-R2 — Wrong-layer fixture fix (B12-NEW-2 + B12-NEW-3)

| Field | Value |
|---|---|
| Date | 2026-05-06 |
| Classification | 🔵 不具合修正 (test fixture correction, NOT spec change) |
| File changed | `tests/test_replay_skill_improver.py` |
| Fixtures changed | `tests/fixtures/llm/skill_improver/*.jsonl` (all 4) |
| Production code changed | None |
| Test count before | 1010 passed |
| Test count after | 1020 passed (suite growth, not fixture additions) |

---

## Audit findings verification

### B12-NEW-2 confirmed

`grep -rn "work_config" src/` returns **no results** — `work_config` does not exist anywhere
in the runtime source tree.  The runtime `prepare` phase emits `improvement_session`
(declared in `src/reyn/stdlib/skills/skill_improver/artifacts/improvement_session.yaml`).
The `copy_to_work` phase header confirms `input: improvement_session`.

The old `_candidate_copy_to_work()` used `schema_name="work_config"` — a type that never
existed at runtime.  The test was self-consistent (the pre-recorded LLM fixture also said
`"type": "work_config"`) so it passed silently while not testing the real flow.

### B12-NEW-3 confirmed

`improvement_session.yaml` requires 9 fields:
`target_skill`, `case_name`, `case_input`, `phase_criteria`, `model`,
`max_iterations`, `score_threshold`, `improvement_focus`, `_resolved_paths`.

The old `iteration_state.session` fixture had only 5 fields and was missing:
`target_skill`, `case_name`, `case_input`, `phase_criteria`, `model`, `improvement_focus`,
and critically `_resolved_paths` — the exact field whose absence caused B10-NEW-1.

---

## Fixture corrections applied

### B12-NEW-2 — `_candidate_copy_to_work()`

**Before** (schema_name and 4 non-existent fields):
```python
schema_name="work_config",
artifact_schema={
    "properties": {
        "skill_path": ..., "work_path": ...,
        "score_threshold": ..., "max_iterations": ...
    },
    "required": ["skill_path", "work_path", "score_threshold", "max_iterations"],
},
```

**After** (real runtime schema, 8 required fields, no path fields):
```python
schema_name="improvement_session",
artifact_schema={
    "properties": {
        "target_skill": ..., "case_name": ..., "case_input": ...,
        "phase_criteria": ..., "model": ..., "max_iterations": ...,
        "score_threshold": ..., "improvement_focus": ...
    },
    "required": ["target_skill", "case_name", "case_input",
                 "phase_criteria", "model", "max_iterations",
                 "score_threshold", "improvement_focus"],
},
```

Note: `_resolved_paths` is intentionally absent — per `prepare.md`, the LLM must NOT
construct path fields; they are injected by the `copy_to_work` preprocessor in the next
phase.

**LLMReplay fixtures rekeyed** (SHA-256 key changes because candidate_outputs are
serialized into the user-turn message):
- `prepare_phase.jsonl`: `aec075c1...` → `e8939707...`
- `force_decide.jsonl`: `6950835c...` → `9fc59114...`
- `validation_fails_after_attempt.jsonl`: `0d0fda3b...` → `f7ba2ebf...`

Response content also updated from `"type": "work_config"` → `"type": "improvement_session"`
with corrected field names.

### B12-NEW-3 — `iteration_state.session` fixture

**Before** (5 fields, missing _resolved_paths and 4 others):
```python
"session": {
    "target_skill_path": "dsl/skills/article_generator",
    "target_dsl_root": ".reyn/skill_improver_work/article_generator/",
    "original_dsl_root": "dsl/skills/article_generator/",
    "score_threshold": 0.85,
    "max_iterations": 3,
},
```

**After** (all 9 required fields including `_resolved_paths`):
```python
"session": {
    "target_skill": "article_generator",
    "case_name": "default",
    "case_input": "Write a short article about AI trends.",
    "phase_criteria": [...],
    "model": "standard",
    "max_iterations": 3,
    "score_threshold": 0.85,
    "improvement_focus": "",
    "_resolved_paths": {
        "target_skill_path": ".reyn/skill_improver_work/article_generator/skill.md",
        "target_dsl_root": ".reyn/skill_improver_work/article_generator",
        "eval_spec_path": "dsl/skills/article_generator/eval.md",
        "original_dsl_root": "dsl/skills/article_generator",
    },
},
```

**LLMReplay fixture rekeyed**:
- `improvement_makes_worse.jsonl`: `09f7120c...` → `2da7d1ca...`

Response content preserved (the `rollback` artifact in the response is unaffected by the
session shape change — it's the test input that changed, not the expected output).

---

## Test assertions updated

- `test_prepare_phase_produces_work_config` renamed to `test_prepare_phase_produces_improvement_session`
- Assertions changed from `artifact["type"] == "work_config"` / `"skill_path" in cfg` etc.
  to `artifact["type"] == "improvement_session"` / `"target_skill" in cfg` etc.
- `prior_attempts[0]["raw"]` in `test_validation_fails_after_attempt_force_decides` updated
  to use `improvement_session` type and correct error message
- `force_decide` test: added `assert artifact["type"] == "improvement_session"`

---

## Verification result

```
1020 passed, 2 xfailed
```

`test_improvement_regression_handled` remains xfail (pre-existing dogfood bug in
`apply_improvements` phase logic, unrelated to fixture structure).

All fixture corrections are purely structural (wrong-layer trap removal).
No production code was modified.
