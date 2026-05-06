# B12-M2 — Tier 2 Fixture Audit (wrong-layer trap prevention)

| Field | Value |
|---|---|
| Date | 2026-05-06 |
| main HEAD | `c7c09fa` |
| Files audited | 30 |
| Confidence-high mismatches | 2 |
| Confidence-medium mismatches | 2 |
| Total B12-NEW-N candidates | 4 |

## Audit method

For each in-scope test file (skill-related, preprocessor, permission, op-runtime,
phase/artifact tests), the following was done:

1. Identified all fixture dict literals and factory functions that construct
   artifact objects passed to functions under test.
2. Compared fixture shapes against the corresponding runtime source code
   (`src/reyn/stdlib/skills/*/`, `src/reyn/kernel/`, `src/reyn/chat/`) to find
   structural mismatches.
3. Classified mismatches as confidence-high (clear mismatch with code reading)
   or confidence-medium (probable mismatch, requires verification).
4. Classified fixtures that are verified-correct (fixture matches runtime shape).

The G17 wrong-layer trap pattern is: test fixture uses `{"type": "X", "data": {...}}`
but runtime OS passes a flat dict (no `data` wrapper), or vice versa, causing tests
to pass while production code fails.

**No source code was modified.** This is an audit-only document.

---

## Files audited (table)

| File | Tier | Fixtures | Mismatches | Confidence |
|---|---|---|---|---|
| `test_eval_builder_path_resolution.py` | 2 | 9 | 0 | — verified correct |
| `test_g16_eval_builder_routing_wording.py` | 1 | 0 (DSL string checks only) | 0 | — not applicable |
| `test_skill_improver_decide_format.py` | 2 | 0 (phase MD content checks) | 0 | — not applicable |
| `test_skill_improver_stdlib_read_perm.py` | 2 | 2 | 0 | — verified correct |
| `test_copy_to_work_phase.py` | 2 | 0 (frontmatter checks only) | 0 | — not applicable |
| `test_copy_to_work_preprocessor.py` | 2b | 1 (`_make_artifact`) | 0 | — verified correct |
| `test_copy_to_work_validation_judgment.py` | 3a | 1 | 1 | medium |
| `test_preprocessor_typing_anyof.py` | 2 | 6 | 0 | — verified correct |
| `test_op_purity_classification.py` | 2 | 0 (enum checks only) | 0 | — not applicable |
| `test_op_runtime_file_permissions.py` | 2 | 5 | 0 | — verified correct |
| `test_permissions.py` | 2 | 3 | 0 | — verified correct |
| `test_permission_denied_audit.py` | 2 | 5 | 0 | — verified correct |
| `test_phase_permissions_rejected.py` | 2 | 2 | 0 | — verified correct |
| `test_skill_permissions_explicit.py` | 2 | 2 | 0 | — verified correct |
| `test_router_system_prompt.py` | 2 | 4 | 0 | — verified correct |
| `test_replay_eval_builder.py` | 3a | 3 | 0 | — verified correct |
| `test_replay_skill_improver.py` | 3a | 4 | 2 | **HIGH** |
| `test_replay_skill_router.py` | 3a/2 | 5 | 0 | — verified correct |
| `test_replay_read_local_files.py` | 3a | 3 | 0 | — verified correct |
| `test_runtime_skill_registry_integration.py` | 2 | 2 | 0 | — verified correct |
| `test_session_skill_registry_integration.py` | 2 | 2 | 0 | — verified correct |
| `test_skill_discard_action.py` | 2 | 1 | 0 | — verified correct |
| `test_nested_skill_path.py` | 2 | 3 | 0 | — verified correct |
| `test_skill_paths_resolution.py` | 1 | 0 (exception contract only) | 0 | — not applicable |
| `test_skill_paths_eval_md.py` | 2 | 4 | 0 | — verified correct |
| `test_skill_postprocessor_executor.py` | 2 | 4 | 1 | medium |
| `test_skill_postprocessor_model.py` | 2 | 3 | 0 | — verified correct |
| `test_workspace_glob_stdlib_perm.py` | 2 | 3 | 0 | — verified correct |
| `test_improvement_session_schema.py` | 2 | 2 | 0 | — verified correct |
| `test_router_invoke_skill_enum.py` | 2 | 1 | 0 | — verified correct |

---

## Confidence-high findings (= B12-NEW-N candidates, fix priority HIGH)

### B12-NEW-1: `test_replay_skill_improver.py`:49 — `work_config` artifact type

**File**: `tests/test_replay_skill_improver.py`, line 45–61, 76–131  
**Confidence**: HIGH  
**Impact**: Tier 3a LLM replay tests pass against stale `work_config` artifact schema while the actual skill emits `improvement_session`.

**The mismatch**:

The test defines `_candidate_copy_to_work()` with `schema_name="work_config"` and a schema containing `skill_path`, `work_path`, `score_threshold`, `max_iterations` (lines 45–61). Tests then assert on `artifact["data"]` having these fields (e.g. line 124: `assert "skill_path" in cfg`).

However, at runtime the `prepare` phase of `skill_improver` is declared to emit `improvement_session` (`src/reyn/stdlib/skills/skill_improver/phases/prepare.md` line 4: `input: user_message | improvement_session`; `skill.md` line 16: `prepare: [copy_to_work]`). The `copy_to_work` phase input is `improvement_session` (not `work_config`). There is no `work_config` artifact type defined anywhere under `src/reyn/stdlib/skills/skill_improver/artifacts/`.

`improvement_session` schema (from `artifacts/improvement_session.yaml`) requires:
`target_skill`, `case_name`, `case_input`, `phase_criteria`, `model`, `max_iterations`, `score_threshold`, `improvement_focus`, `_resolved_paths` — none of which appear in the test's `work_config` schema.

**Root cause**: The test was written against an earlier design iteration where `prepare` emitted a `work_config`. The skill was redesigned to carry `improvement_session` through all phases, but the replay test was not updated.

**Evidence that this is a wrong-layer trap**: the `test_prepare_phase_produces_work_config` and related tests pass because the pre-recorded LLM fixture produces output conforming to the test-local `work_config` schema (which was recorded when `work_config` was the real artifact). At runtime, `OSRuntime` would validate the LLM output against the real `improvement_session` schema via the phase's `input_schema`, not the test-local schema.

### B12-NEW-2: `test_replay_skill_improver.py`:338–446 — `iteration_state.session` truncated shape

**File**: `tests/test_replay_skill_improver.py`, line 338–446  
**Confidence**: HIGH  
**Impact**: Tier 3a replay test for `apply_improvements` passes a skeletal `session` sub-object inside `iteration_state` that is missing most fields required by the real `improvement_session` schema.

**The mismatch**:

The fixture (line 365–390) passes:
```python
input_artifact={
    "type": "improvement_plan",
    "data": {
        "summary": ...,
        "changes": [],
        "iteration_state": {
            "current_iteration": 2,
            "latest_eval": {"overall_score": 0.55, "weakest_phase": "generate_article"},
            "session": {
                "target_skill_path": "dsl/skills/article_generator",
                "target_dsl_root": ".reyn/skill_improver_work/article_generator/",
                "original_dsl_root": "dsl/skills/article_generator/",
                "score_threshold": 0.85,
                "max_iterations": 3,
            },
            "history": [{"iteration": 1, "eval_score": 0.72, ...}]
        },
    },
}
```

The `iteration_state.yaml` schema declares `session` as the carried-through `improvement_session` object, which `improvement_session.yaml` requires:
`target_skill`, `case_name`, `case_input`, `phase_criteria`, `model`, `max_iterations`, `score_threshold`, `improvement_focus`, `_resolved_paths`.

The test fixture's `session` sub-object has only 5 fields — specifically it omits:
- `target_skill` (short skill name used by OS resolver)
- `case_name`, `case_input`, `phase_criteria`, `model`, `improvement_focus`
- `_resolved_paths` (critical — OS-resolved path fields for file writes)

The `apply_improvements` phase uses `session._resolved_paths` to determine target write paths. Without `_resolved_paths`, any Tier 2 test derived from this fixture would pass while the runtime phase would fail with missing path info (the same pattern as B10-NEW-1).

This is HIGH because `apply_improvements` is the most path-sensitive phase, and the fixture omits the exact field (`_resolved_paths`) that caused the B10-NEW-1 bug.

---

## Confidence-medium findings

### B12-NEW-3: `test_copy_to_work_validation_judgment.py`:104 — `_resolved_paths` sub-structure

**File**: `tests/test_copy_to_work_validation_judgment.py`, line 104–131  
**Confidence**: MEDIUM

The test fixture includes `"_resolved_paths"` with sub-fields:
```python
"_resolved_paths": {
    "target_skill_path": ".reyn/skill_improver_work/direct_llm/skill.md",
    "target_dsl_root": ".reyn/skill_improver_work/direct_llm",
    "eval_spec_path": "reyn/local/direct_llm/phases/eval.md",
    "original_dsl_root": "reyn/local/direct_llm",
},
```

The `inject_resolved_paths` in `copy_to_work.py` (runtime) returns:
```python
{
    "target_skill_path": work_dir + "/skill.md",  # work copy path
    "target_dsl_root": work_dir,
    "eval_spec_path": prep.get("eval_spec_path"),
    "original_dsl_root": original_dsl_root,
}
```

The `eval_spec_path` in the fixture is `"reyn/local/direct_llm/phases/eval.md"` (with `/phases/` in the path), but the runtime `compute_paths` in `copy_to_work_resolver.py` derives it as `skill_dir_str + "/eval.md"` (directly under skill dir, not under phases). This is a probable mismatch in the `eval_spec_path` field. Since this is a Tier 3a replay test and the fixture is hand-crafted, the field value may be wrong in a way that is invisible to the replay but would fail at runtime.

**Confidence medium** because: (a) it's a Tier 3a test where the fixture is hand-crafted so structural correctness is not verified by LLM execution; (b) the mismatch is in a nested field value path string, not the wrapper structure.

### B12-NEW-4: `test_skill_postprocessor_executor.py`:116 — `wrapped=True` artifact format assumption

**File**: `tests/test_skill_postprocessor_executor.py`, line 115–117  
**Confidence**: MEDIUM

The `_artifact()` helper:
```python
def _artifact(y: str = "hello") -> dict:
    return {"type": "llm_art", "data": {"y": y}}
```

This matches the `wrapped=True` artifact format. The `ArtifactDef` for `llm_art` is declared with `wrapped=True` in `_build_skill()` (line 68–77). This is correct for the current `expand_skill` / `PostprocessorExecutor` contract.

However, the postprocessor output schema validation uses `output_schema` as a raw dict (not the wrapped schema). If `output_schema` is specified as an artifact name (`"post_art"` — tested in `test_skill_postprocessor_artifact_name_reference`), `expand_skill` wraps it into `{type: "post_art", data: {...}}` form. The `PostprocessorExecutor` then validates the finish artifact against `skill.postprocessor.output_schema`.

The concern: `test_postprocessor_output_schema_final_validation_fails` (line 202–223) validates a `{"type": "llm_art", "data": {"y": "hello"}}` artifact against `output_schema = {"type": "object", "required": ["caller_extra"], ...}` — the schema is applied to the *full wrapped artifact* or to `data` only? This is ambiguous and may hide a validation path mismatch. Confidence medium because this requires reading `PostprocessorExecutor.run()` validation logic carefully; the risk exists but requires investigation.

---

## False alarms / verified-correct fixtures

**27 test files, 0 mismatches found.** Notable verified-correct cases:

- `test_eval_builder_path_resolution.py`: Both wrapped (`{"type": "eval_builder_request", "data": {...}}`) and unwrapped (`{"target_skill": "..."}`) forms tested. Matches `_extract_skill_name` priority 1 (top-level) and priority 2 (wrapped) logic. The B9-NEW-2 fix is correctly represented.
- `test_copy_to_work_preprocessor.py`: `_make_artifact` uses `{"type": "improvement_session", "data": {"target_skill": ...}}` which matches `copy_to_work_resolver.py`'s `artifact.get("data", {}).get("target_skill", "")`.
- `test_skill_paths_eval_md.py`: All `compute_paths` calls use `{"type": "improvement_session", "data": {"target_skill": ...}}` wrapper form, matching the runtime path.
- `test_permission_denied_audit.py`, `test_op_runtime_file_permissions.py`: Use `FileIROp` model objects (not raw dicts), so no wrong-layer risk.
- `test_improvement_session_schema.py`: Tests `_strip_data` directly against the real `improvement_session.yaml` schema. The fixture data is a flat data dict (not wrapped), which is the correct input to `_strip_data` (which operates on `artifact.data`, not the full artifact).
- `test_preprocessor_typing_anyof.py`: Uses schema objects only (no artifact fixtures). No wrong-layer risk.
- `test_replay_eval_builder.py`: Uses `{"type": "user_message", "data": {"text": ..., "skill_path": ...}}` which matches `analyze_skill_resolver.py`'s supported forms (data.text regex fallback). Correct.
- `test_replay_read_local_files.py`: Uses `{"type": "user_message", "data": {"text": ...}}` and `{"type": "read_plan", "data": {...}}`. Both match the `read_local_files` skill artifact declarations.

---

## Recommendation for batch 13+

Priority order for fix dispatch:

| Priority | Candidate | Action |
|---|---|---|
| **HIGH** | B12-NEW-1: `test_replay_skill_improver.py` `work_config` schema | Verify actual runtime artifact type emitted by `prepare` phase via Tier 3 LLMReplay against real schema. If `improvement_session` is correct, update `_candidate_copy_to_work()` and re-record fixture. |
| **HIGH** | B12-NEW-2: `test_replay_skill_improver.py` `improvement_plan.iteration_state.session` | Verify `iteration_state.yaml` schema against fixture shape. Specifically check whether `session` sub-object has all required fields or just the 4-field subset in the test. |
| **MED** | B12-NEW-3: `test_copy_to_work_validation_judgment.py` `eval_spec_path` value | Check `eval_spec_path` derivation in `copy_to_work_resolver.py` vs fixture value. The `/phases/` in fixture path looks wrong; runtime generates `<skill_dir>/eval.md` (no `/phases/`). |
| **MED** | B12-NEW-4: `test_skill_postprocessor_executor.py` output_schema scope | Verify whether `PostprocessorExecutor` final validation applies `output_schema` to `data` only or to the full wrapped `{"type", "data"}` artifact. Adjust fixture if mismatch found. |

**Meta recommendation**: Before dispatching fixes, follow the verify-first discipline (batch 10 retro): run a Tier 3 e2e retest to confirm whether B12-NEW-1/2 are true regressions or resolved-indirectly. The `work_config` fixture in particular may be from a very early design that was superseded by actual development — it may be a test-artifact-only obsolescence rather than a runtime regression.
