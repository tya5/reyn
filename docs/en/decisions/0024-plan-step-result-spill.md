# ADR-0024: Plan step result spill-to-file (R-D10 mirror)

**Status**: Accepted (2026-05-08)
**Track**: Plan-mode persistence — closes ADR-0023 "Open issues:
Step result size cap" deferred item.

## Context

ADR-0023 Phase 2 v1 stored each plan step's text output inline on
`PlanSnapshot.step_results: dict[str, str]` and bounded each entry at
32 KB with a `[truncated]` suffix:

```python
# src/reyn/plan/plan_registry.py
_STEP_RESULT_MAX_CHARS = 32_768
_STEP_RESULT_TRUNC_SUFFIX = "\n[truncated]"

def _bound_step_result(text: str) -> str:
    if len(text) <= _STEP_RESULT_MAX_CHARS:
        return text
    cap = _STEP_RESULT_MAX_CHARS - len(_STEP_RESULT_TRUNC_SUFFIX)
    return text[:cap] + _STEP_RESULT_TRUNC_SUFFIX
```

ADR-0023's "Open issues" listed this as deferred:

> **Step result size cap.** `step_results: dict[str, str]` could grow
> pathologically (= multi-page web scrape). Phase 2 v1: bound at write
> time (= 32KB truncation with "[truncated]" suffix). Spill-to-side-
> files pattern (R-D10 mirror) deferred.

The 32 KB bound exists for three reasons:
1. **Atomic save cost** — `PlanSnapshot.save` does
   `tmp + fsync + rename` on every step completion. Inline multi-MB
   text would slow this down materially on long plans.
2. **Memory footprint on restart** — `PlanRegistry.load_active`
   materialises every per-plan snapshot in `dict[plan_id, PlanSnapshot]`.
3. **Threshold consistency with R-D10** — skill-side
   `step_completed.result` payloads ≥32 KB already spill via
   `llm_result_ref.py`.

The lossy nature of the bound is the cost: workflows where a single
step produces >32 KB (multi-page scrape, long code generation,
comprehensive analysis) silently corrupt downstream steps that depend
on the truncated text.

## Considered alternatives

- **A. Always spill every step result to file.** Uniform code path
  but adds disk I/O for every step (= chat-like steps that produce a
  paragraph pay an open/write/fsync round-trip).
- **B. Hybrid spill (= R-D10 mirror).** Inline ≤32 KB; spill >32 KB
  to a per-plan workspace file; reads transparent via an accessor.
  **Adopted.**
- **C. Larger inline bound + lossy fallback.** Raise the cap to
  e.g. 256 KB; still lossy past that. Postpones the problem without
  solving it.

## Decision

Adopt **B**: hybrid spill mirroring `llm_result_ref.py` skill-side.

### 1. Schema (additive, no version bump)

`PlanSnapshot` grows one field, alongside the existing
`step_results: dict[str, str]`:

```python
# Existing
step_results: dict[str, str] = {}      # step_id → inline text (≤ THRESHOLD chars)

# NEW — additive, additive, optional
step_result_refs: dict[str, str] = {}  # step_id → relative path (chars > THRESHOLD)
```

`PLAN_SNAPSHOT_VERSION` stays at **1**. The new field defaults to
empty for old snapshot files — backward-compatible load. New snapshot
files written by post-ADR-0024 code remain readable by pre-ADR code
because old code ignores unknown JSON keys (= forward-compatible at
the format layer; the only loss is that pre-ADR code can't read the
spilled content for any plan whose results spilled, which is a niche
downgrade scenario).

The 32 KB cap stays as the **inline-vs-spill threshold** but is no
longer truncating — anything over it goes to file with full fidelity.

### 2. Storage layout

Per-plan directory already exists from ADR-0023 §3.5:

```
.reyn/agents/<name>/state/plans/<plan_id>/
├── decomposition.json                    ← existing (ADR-0023 step 1)
└── step_results/                         ← NEW (this ADR)
    ├── s1.txt                            ← step output (raw text, UTF-8)
    ├── s2.txt
    └── s3.txt
```

`step_results/<step_id>.txt` is **plain UTF-8 text** — no JSON
envelope, no checksums. Rationale: matches the data shape (the value
is a Python `str`), keeps read-side simple (`Path.read_text`), and
avoids gratuitous serialisation overhead. Crash-during-write yields a
partial file; resume falls back to `step_result_file_missing` failure
classification (see §4 below).

Atomic write recipe mirrors `PlanSnapshot.save`:
`tmp + fsync + rename`. Snapshot is rewritten *after* the file
write succeeds so a crash between the two leaves the file orphaned
but the snapshot still in pre-write state.

### 3. Code path

#### 3.1 Write path (`PlanRegistry.record_step_completed`)

```python
async def record_step_completed(self, *, plan_id, step_id, applied_seq, result_text):
    snap = self._snapshots.get(plan_id)
    if snap is None: return
    # existing applied_seq / last_step_applied_seq bumps...

    if len(result_text) <= _SPILL_THRESHOLD:
        snap.step_results[step_id] = result_text
        snap.step_result_refs.pop(step_id, None)        # if was a re-run that previously spilled
    else:
        path = self._step_result_path(plan_id, step_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # atomic write
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(result_text)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
        snap.step_results.pop(step_id, None)            # clear inline if any
        snap.step_result_refs[step_id] = f"step_results/{step_id}.txt"

    # existing last_committed_step_id, save, hook fire...
```

`_bound_step_result` is **removed** — no truncation anywhere.

#### 3.2 Read path (accessor)

A new module-level helper:

```python
# plan_snapshot.py
def get_step_result(snap: PlanSnapshot, agent_state_dir: Path,
                    step_id: str) -> str | None:
    """Return the step's recorded text, or None if not recorded.

    Reads inline first (= cheap path for ≤ threshold), falls back to
    file ref. Missing-file → None (= caller treats as not-recorded).
    """
    if step_id in snap.step_results:
        return snap.step_results[step_id]
    rel = snap.step_result_refs.get(step_id)
    if rel is None:
        return None
    path = agent_state_dir / "plans" / snap.plan_id / Path(rel).name
    # actually: full relative resolution under per-plan dir
    full = agent_state_dir / "plans" / snap.plan_id / rel
    try:
        return full.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return None
```

`PlanResumeAnalyzer.analyze` and `execute_plan` memo replay both
route through `get_step_result` instead of reading `snap.step_results`
directly.

#### 3.3 Cleanup path (`PlanRegistry.complete` + `/plan discard`)

`reyn.plan.decomposition.delete_decomposition` currently removes
`decomposition.json` and the per-plan directory only if empty. With
`step_results/` subdirectory present the directory is no longer empty
on the cleanup path, so it would orphan.

**New helper** in `decomposition.py`:

```python
def delete_plan_workspace(agent_state_dir: Path, plan_id: str) -> bool:
    """Recursively remove the per-plan directory + everything in it.

    Idempotent. Returns True if the directory existed.
    """
    plan_dir = decomposition_dir(agent_state_dir, plan_id)
    if not plan_dir.exists():
        return False
    shutil.rmtree(plan_dir, ignore_errors=True)
    return not plan_dir.exists()
```

`delete_decomposition` keeps its existing single-file delete behaviour
(= no breaking change for code paths that only want the decomposition
file gone). `PlanRegistry.complete` is updated to call
`delete_plan_workspace` when `delete_artifact=True` so the entire
workspace dir (decomposition + step_results) is reclaimed atomically.

`/plan discard` already calls `session.delete_plan_decomposition`;
that will be updated to call the workspace-wide cleanup so spilled
files are removed.

#### 3.4 Reset path (`reset_from_step`)

Already clears `snap.step_results` for the target step and after.
With this ADR it must also:

- Clear `snap.step_result_refs[sid]` for cleared steps
- Delete `step_results/<sid>.txt` files for cleared steps (= prevent
  stale spilled content from being read on the next launch)

### 4. Crash semantics

`step_results/<step_id>.txt` write failures are graceful:

| Crash window | State on disk | Resume behaviour |
|---|---|---|
| Before file write starts | Snapshot has neither inline nor ref | Step classifies as `pending` (= re-execute) |
| Mid-write (`.tmp` partial) | `.tmp` exists, `step_result_refs` not yet bumped | Same as above (= ref lookup empty → pending) |
| After rename, before snapshot save | File exists, `step_result_refs` not yet bumped | File orphaned; cleaned by `delete_plan_workspace` on plan completion / discard |
| After snapshot save, file later corrupt or unreadable | Ref present, file unreadable | `get_step_result` returns None → analyzer pairs as `failed("step_result_file_missing")` → coordinator `discard` policy applies (= safe) |

The accessor's None return on read-failure is the safety valve. Callers
treat None uniformly as "not recorded" — the analyzer maps it to
`failed` with a descriptive `error_message`, and the discard coordinator
path surfaces the situation to the operator via outbox notice.

### 5. Migration

No version bump. Existing snapshots load with `step_result_refs={}`
(= empty dict default). Existing inline `step_results` keep working.
Only newly-written results that exceed the threshold spill.

Old plans' inline truncated content (= `[truncated]` suffix) is left
as-is; on resume the analyzer sees the truncated content as the
step's result (= same Phase 2 v1 behaviour). Operators who want the
old plans re-run with full output use `/plan resume --from <step_id>`
to clear the truncated entry and re-execute.

## Consequences

### Positive

- **No silent data loss.** Step output of any size is preserved.
- **Snapshot stays small.** Atomic-save cost remains constant
  regardless of step output size.
- **R-D10 alignment.** Plan-side spill mirrors skill-side spill;
  future work could unify the helpers (`llm_result_ref.py` +
  `step_result_ref` style helper) into a generic `WorkspaceRef`.
- **No schema migration drama.** Additive field, no version bump,
  old + new snapshots interoperate.

### Negative

- **Two read paths.** Inline vs file. Mitigated by the
  `get_step_result` accessor — call sites stay simple.
- **Disk I/O on large step.** Each >32 KB step pays open + write +
  fsync. Negligible compared to the LLM call that produced the text.
- **Crash window between file rename and snapshot save** orphans the
  file. Handled by workspace-wide cleanup on plan completion /
  discard. Worst case: a few orphan files until next plan_completed
  / `/plan discard`.

### Open issues / explicit non-goals

- **Compression.** Spilled files are raw UTF-8. If a workflow
  routinely produces multi-MB step results (= unusual for Reyn's
  intended scope), gzip is straightforward future work.
- **Per-step retention policy.** Currently spilled files live until
  plan completion. A "bounded retention by N most-recent" policy
  could be added if very long plans (= ≥ 50 steps) run.
- **Generic `WorkspaceRef` helper.** Plan-side spill duplicates
  some structure with skill-side `llm_result_ref.py`. Unification
  deferred until both paths see independent maturity.

## Cross-references

- **ADR-0001** (state model — WAL + snapshot): snapshot stays cache;
  spilled files are workspace-side.
- **ADR-0023** (plan-mode forward replay): closes the §"Open issues:
  Step result size cap" deferred item.
- **R-D10** (LLM result payload size handling): same hybrid pattern,
  skill-side. `src/reyn/skill/llm_result_ref.py` is the implementation
  reference.
- **P5** (Workspace is the single source of truth): step results
  belong in workspace, not snapshot cache. This ADR aligns plan-mode
  with that invariant.
