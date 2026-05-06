# B12 Step 3 — N=5 stability retest (real milestone confirmation)

| Field | Value |
|---|---|
| Date | 2026-05-06 |
| main HEAD | `2219b20` |
| Fixes active | B11-NEW-1 (R1) + B11 wave (R2/R3) + earlier batch fixes |
| Sample size | N=5 |
| Complete rate | 0/5 (0%) |
| Real milestone | not-yet |

## Per-session verdicts

<table>
<thead>
<tr>
<th>Session</th>
<th>Verdict</th>
<th>Router action</th>
<th>Furthest phase reached</th>
<th>Stopping reason</th>
<th>Cost (est.)</th>
</tr>
</thead>
<tbody>
<tr>
<td>Run 1</td>
<td><strong>partial</strong></td>
<td>invoke_skill directly (R3 fix effective)</td>
<td>prepare → copy_to_work (start)</td>
<td>copy_to_work preprocessor step[0] python trusted step denied — non-interactive mode, no startup_guard pre-approval for python steps (B12-NEW-1)</td>
<td>~$0.0006</td>
</tr>
<tr>
<td>Run 2</td>
<td><strong>partial</strong></td>
<td>invoke_skill directly</td>
<td>prepare → copy_to_work (start)</td>
<td>same as Run 1 — copy_to_work step[0] trusted python denied</td>
<td>~$0.0006</td>
</tr>
<tr>
<td>Run 3</td>
<td><strong>partial</strong></td>
<td>invoke_skill directly</td>
<td>prepare → copy_to_work (start)</td>
<td>copy_to_work step[0] trusted python denied + prepare phase validation error (secondary)</td>
<td>~$0.0006</td>
</tr>
<tr>
<td>Run 4</td>
<td><strong>partial</strong></td>
<td>invoke_skill directly</td>
<td>prepare → copy_to_work (start)</td>
<td>same as Run 1 — copy_to_work step[0] trusted python denied</td>
<td>~$0.0005</td>
</tr>
<tr>
<td>Run 5</td>
<td><strong>partial</strong></td>
<td>invoke_skill (multiple attempts — 4 router turns)</td>
<td>prepare → copy_to_work (start)</td>
<td>copy_to_work step[0] trusted python denied + prepare validation errors (secondary)</td>
<td>~$0.0012</td>
</tr>
</tbody>
</table>

## Aggregated metrics

| Metric | Value |
|---|---|
| complete (6-phase full run) | 0/5 (0%) |
| partial (past prepare, stopped at copy_to_work) | 5/5 (100%) |
| routing-fail (no skill invoked) | 0/5 (0%) |
| router-fail (invoked but immediate fail before prepare) | 0/5 (0%) |
| Most common stopping point | copy_to_work preprocessor step[0] (all 5 sessions) |
| Routing pattern | invoke_skill direct (all 5 sessions — R3 fix effective) |

### R1 fix (stdlib_root default read zone) observed effect

- B11-NEW-1 (step[1] file.read permission_denied) is **NOT observed** in any session
- R1 fix is effective: the stdlib path read permission no longer blocks
- However, a new blocker appeared at step[0] (python trusted step) — see B12-NEW-1

### R3 fix (router direct invoke) observed effect

- 5/5 sessions: router correctly dispatched invoke_skill
- 0/5 routing-fail — a major improvement from B11's 3/5 (60%) routing-fail rate
- R3 fix is now consistently effective in this N=5 sample

### B12-NEW-1: trusted python step denied in non-interactive mode

**Error**: `Phase 'copy_to_work' preprocessor step[0] python ./copy_to_work_resolver.py:compute_paths: trusted python step ./copy_to_work_resolver.py:compute_paths denied by user`

**Root cause**: In non-interactive mode (piped stdin / `sys.stdin.isatty() == False`), `startup_guard` auto-approves `file.read` entries declared in `skill.md` but does **not** auto-approve `python` steps. When `copy_to_work` starts, `require_python_step()` calls `_approve()` which returns `False` in non-interactive mode (no persisted approval exists, no interactive prompt available).

**Relationship to B11-NEW-1**: B11-NEW-1 blocked at step[1] (file.read); the R1 fix resolved that. The previously masked step[0] (python trusted) is now the first blocker. This issue was present before R1 but hidden behind step[1].

**Affected path**: `PermissionResolver.require_python_step()` → `_approve()` → `return False` (non-interactive, no saved key)

**Why `--allow-untrusted-python` is insufficient**: The flag sets `_trusted_python_allowed=True` (bypasses the "flag not provided" hard-fail check), but the subsequent `_approve()` call still requires interactive prompt or persisted approval. The flag enables the path but does not bypass the `_approve()` gate.

**Fix direction**: One of:
- In non-interactive mode, auto-approve python steps declared in `skill.md` (same as file.read auto-approval) — treat skill.md declaration as checked-in consent
- Add `--yes-python` / `--auto-approve-python` flag that pre-approves all declared python steps
- Write a `.reyn/approvals.yaml` with the required keys before the dogfood run

## Delta vs batch 11 (0/5 complete)

| Metric | Batch 11 N=5 | Batch 12 S3 N=5 | Delta |
|---|---|---|---|
| complete rate | 0% (0/5) | 0% (0/5) | 0pp |
| routing-fail rate | 60% (3/5) | 0% (0/5) | **-60pp** (major improvement) |
| partial rate | 40% (2/5) | 100% (5/5) | +60pp |
| dominant blocker | copy_to_work step[1] file.read (B11-NEW-1) | copy_to_work step[0] python trusted (B12-NEW-1) | different blocker |

**Key shift**: R1 fix and R3 fix together eliminated both the file.read blocker and the routing-fail pattern. All 5 sessions now reach `copy_to_work` — a structural advancement. The new hard blocker is the python trusted approval gate in non-interactive mode.

## Real milestone verdict

**not-yet** — complete rate 0/5 (0%), below ≥3/5 (60%) threshold.

However, the improvement structure is significant:
- Routing now works 5/5 (was 2/5 in B11, 0/5 routing-fail is new)
- All sessions reach copy_to_work (was 2/5 in B11)
- New blocker (B12-NEW-1) is a deterministic structural issue, not LLM non-determinism
- B12-NEW-1 has a clear fix path (non-interactive python auto-approval)

Pending B12-NEW-1 fix, a B13 N=5 retest is warranted for milestone confirmation.

## New bugs (B12-NEW-N)

### B12-NEW-1 (CRITICAL): trusted python step denied in non-interactive dogfood runs

**Summary**: `copy_to_work` preprocessor step[0] (`./copy_to_work_resolver.py:compute_paths`, mode=trusted) is always denied in piped-stdin (non-interactive) mode because `startup_guard` does not auto-approve python steps the same way it auto-approves file.read.

**Observed**: 5/5 sessions, deterministic

**Severity**: CRITICAL — blocks all skill_improver dogfood runs in non-interactive mode

**Fix batch**: B13 (next batch)

**Note**: Secondary failures observed in runs 3 and 5 (`prepare` phase validation errors: `_resolved_paths` fields None), but these appear to be retry-exhaustion effects after the trusted python step failures. B12-NEW-1 is the primary root cause.
