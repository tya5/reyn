# B13-R1 — Revert G15 non-interactive auto-approve (651a053)

| Field | Value |
|---|---|
| Date | 2026-05-04 |
| Revert target | `651a053` (G15 fix) |
| Batch | B13 R1 |
| Status | complete |

## What was reverted — Change (1): non-interactive auto-approve

**File**: `src/reyn/permissions/permissions.py` — `startup_guard()`

G15 introduced a branch in `startup_guard` that, in non-interactive mode,
called `session_approve_path` directly for every declared `file.read` path
outside the default zone.  This bypassed the documented approval requirement:

> Non-interactive runs: Approvals must be in place beforehand: either
> pre-approved in `reyn.yaml` or persisted to `.reyn/approvals.yaml` from a
> prior interactive run.

The auto-approve block treated the `skill.md` declaration as implicit consent.
The doc says no such mechanism exists — only layer 3 (`reyn.yaml` / `reyn.local.yaml`)
or persisted layer 2 (`.reyn/approvals.yaml`) approvals are valid in
non-interactive runs.

**Revert action**: removed the `if not self._interactive:` auto-approve block
(~15 lines).  Updated docstring to match documented design.

## What was kept — Change (2): invoke_sub_skill resolver propagation

**Files**: `src/reyn/skill/sub_skill_runner.py`, `src/reyn/op_runtime/run_skill.py`

G15 also added a `permission_resolver` parameter to `invoke_sub_skill` and
propagated it from the parent via `run_skill.py`.

**Decision: KEEP.**

Rationale: without a resolver, the sub-skill's `workspace._resolve_read` raises
`PermissionError` for all paths outside CWD — regardless of what the sub-skill
declared, and regardless of whether approvals were pre-granted in `reyn.yaml`.
The resolver is the mechanism by which any approval (config, saved, session) is
checked; without it the entire permission system is blind to the sub-skill.

This is not a doc violation.  The doc says skill A's approvals do not
transitively grant skill B permissions — that invariant is preserved: each
skill's approvals are keyed by skill name.  Passing the resolver object gives
the sub-skill access to the approval-checking mechanism, not to the parent's
approvals.  The sub-skill still runs its own `startup_guard` (in interactive
mode) and still requires its own pre-approvals in non-interactive mode.

Updated docstring in `sub_skill_runner.py` to remove the claim that
"startup_guard is NOT re-run for sub-skills — declarations auto-approved by
the parent's guard in non-interactive mode" (which was only true with change (1)
present).

## Tests removed

**File deleted**: `tests/test_g15_noninteractive_startup_guard.py` — 7 Tier 2 tests
that pinned the G15 non-interactive auto-approve behavior.  All 7 tested the
removed branch and are no longer valid.

No other test files required updating — the remaining tests in
`test_skill_improver_stdlib_read_perm.py`, `test_workspace_glob_stdlib_perm.py`,
`test_permission_denied_audit.py`, and `test_b11_stdlib_default_read_zone.py`
do not depend on non-interactive auto-approve; they use `session_approve_path`
directly or test the existing denial behavior.

## Test count change

| Metric | Before | After |
|---|---|---|
| passed | 1022 | 1015 |
| xfailed | 2 | 2 |
| removed | — | 7 (G15-specific) |

## User-visible impact

**dogfood automation** (piped stdin / non-TTY runs): no longer auto-approves
declared `file.read` paths.  Requires `reyn.local.yaml` pre-approval (layer 3):

```yaml
# reyn.local.yaml (operator personal config, gitignored)
permissions:
  file.read: allow
```

This is the documented layer 3 mechanism.  Production interactive users:
unaffected (startup_guard still prompts as before).
