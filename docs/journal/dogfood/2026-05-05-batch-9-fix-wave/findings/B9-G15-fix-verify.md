# B9-G15 Fix Verify — eval_builder stdlib path read permission gap

| Field | Value |
|---|---|
| Date | 2026-05-05 |
| Giveup ref | G15 |
| Status | **resolved** |

## Fix landed

Two-part fix in a single commit:

**Part 1 — `startup_guard` non-interactive auto-approve** (`src/reyn/permissions/permissions.py`):  
When `not self._interactive`, `startup_guard` now calls `session_approve_path` for each
declared `file.read` path outside the default zone.  Previously it called `_prompt_file_access`
which immediately returned False in non-interactive mode (Hypothesis A).

**Part 2 — PermissionResolver propagation** (`src/reyn/skill/sub_skill_runner.py`,
`src/reyn/op_runtime/run_skill.py`):  
`invoke_sub_skill` now accepts a `permission_resolver` keyword argument (default None,
backward-compatible) and passes it to `Agent(...)`.  The `run_skill` op handler passes
`ctx.permission_resolver`.  Previously sub-skills had no resolver and `workspace._resolve_read`
denied all paths outside CWD (Hypothesis B).

## Tests added

File: `tests/test_g15_noninteractive_startup_guard.py` — 7 Tier 2 tests

| Test | Guards |
|---|---|
| `test_startup_guard_noninteractive_approves_declared_read_path` | Fix 1 core: startup_guard auto-approves in non-interactive mode |
| `test_startup_guard_noninteractive_recursive_scope_covers_subtree` | Fix 1: recursive scope covers full subtree |
| `test_startup_guard_noninteractive_approval_is_skill_scoped` | Fix 1: approval is per-skill-name (isolation preserved) |
| `test_startup_guard_noninteractive_does_not_approve_write_paths` | Fix 1: write paths NOT auto-approved |
| `test_startup_guard_interactive_still_prompts` | Regression: interactive mode still prompts |
| `test_startup_guard_noninteractive_skips_already_approved_paths` | Fix 1: idempotency / pre-approved paths respected |
| `test_invoke_sub_skill_signature_accepts_permission_resolver` | Fix 2: parameter signature present with correct default |

## Test results

```
986 passed, 2 xfailed (baseline was 979 passed)
```

+7 new tests from this fix.

## Per-fix retest plan

The dogfood chain `skill_improver → eval_builder → analyze_skill` should be retested in B9
with:
- `--allow-untrusted-python` flag (required for Python preprocessor steps)
- piped stdin (non-interactive mode, `--no-tty` or `echo "..." | reyn run ...`)
- target: `direct_llm` stdlib skill

Expected result: `analyze_skill` LLM turns should now read `direct_llm/skill.md` and
`direct_llm/artifacts/*.yaml` without `permission_denied` events.  Chain should proceed to
`write_eval` phase.

The 18 `permission_denied` events observed in B8-S1 should be absent.  If any remain, check:
1. That the resolver is non-None in the sub-skill's workspace (`ctx.permission_resolver`)
2. That the sub-skill's `startup_guard` ran and recorded approvals for `eval_builder`
3. That the resolved absolute path is under the approved directory tree

## giveup-tracker update

G15 status: **active → resolved** at this commit.
