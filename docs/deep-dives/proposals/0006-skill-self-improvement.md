# FP-0006: Skill Self-Improvement — Execution-Trace-Driven + Versioning + Rollback

**Status**: proposed
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

The `skill_improver` stdlib skill already operates as an eval-score-driven improvement loop. This proposal extends it to accept P6 event logs (execution traces) as improvement input. It also adds version saving to `.reyn/skill-versions/`, recording `skill_version_hash` in events, and a `reyn skill rollback` CLI — achieving the same self-improvement as Hermes GEPA, but safely under the Permission model and ask_user approval gate.

---

## Motivation

### Comparison with Hermes GEPA

Hermes Agent (ICLR 2026 Oral) uses GEPA to automatically improve skills from execution traces, reporting a 40% speed improvement on repeated tasks. However, because skill changes happen as side effects outside the OS, tracking changes, enforcing permissions, and rolling back are fundamentally difficult.

In Reyn, the same self-improvement can be achieved while maintaining governance by designing it to "run as a skill and pass through the Permission model."

### Current state of skill_improver

The current `skill_improver` operates with the following phases (unchanged):

```
prepare → copy_to_work → run_and_eval → plan_improvements → apply_improvements → finalize
                                ↑__________________________________|
```

- Improvements are applied to a **copy** of the workspace (the original files are not modified directly)
- Exits when eval score exceeds the threshold (0.85) / regression / stagnation / max_iterations
- The `finalize` phase copies the improved files back to their original locations

**What is being added:**
1. A mode that uses execution traces (P6 event logs) as improvement input
2. Version saving and recording of `skill_version_hash`
3. A user approval gate
4. A rollback CLI

### The problem that versioning solves

The current `skill_improver` has no record of which version ran after an improvement. It cannot answer:

- "Which had a higher success rate, v1 or v2?"
- "The failure rate went up after last week's improvement. I want to revert."
- "Was this run performed before or after the improvement?"

---

## Proposed implementation

### Component A — Add `skill_version_hash` to events (SMALL)

Add the content hash of `skill.md` to the `run_skill_started` emit in `src/reyn/op_runtime/run_skill.py`.

```python
# Before (near run_skill.py:73)
event_log.emit("run_skill_started", skill=skill_name, state_dir=str(state_dir))

# After
skill_hash = _compute_skill_hash(skill_path)  # sha256(skill.md content)
event_log.emit("run_skill_started", skill=skill_name, state_dir=str(state_dir),
               skill_version_hash=skill_hash)
```

Effect: History such as "85% success rate over 50 runs with this hash" accumulates naturally in the P6 event log. The `collect_traces` phase leverages this.

### Component B — `.reyn/skill-versions/` version saving (SMALL)

When the `finalize` phase of `skill_improver` applies the improved skill, it simultaneously saves a version archive.

```
.reyn/skill-versions/
  my_skill/
    v1.md      ← saved on first apply (the original before apply)
    v2.md      ← after 1st improvement applied
    v3.md      ← after 2nd improvement applied
    current    ← "3" (current version number)
```

`.reyn/` is the default write zone, so **no Permission declaration is required**.

When the version count exceeds `self_improvement.max_versions` (default 10), the oldest versions are deleted. However, the version referenced by `current` is never deleted.

### Component C — Execution-trace-driven mode (MEDIUM)

```yaml
# Input parameters for skill_improver (newly added)
improvement_source: traces   # traces | tests | both (default: tests — backward compatible)
trace_lookback_runs: 20      # reference the most recent N runs
```

When `improvement_source` is `traces` or `both`, a new `collect_traces` phase is inserted before `copy_to_work`:

```
prepare → collect_traces → copy_to_work → plan_improvements → apply_improvements → finalize
```

**`collect_traces` phase behavior:**

```markdown
# collect_traces

Collect the execution history of the target skill from the P6 event log and
save an analysis summary useful for improvement to the workspace.

Collection methods (2 options):

① Direct read via read_file(events/*.jsonl) (always available)
  - Filter by skill_version_hash to extract only runs of the target skill
  - Limit to the most recent trace_lookback_runs entries

② Semantic search via recall op (when RAG Phase 1 has landed — events are already indexed by index_docs)
  - query: "failure patterns for skill X / errors in phase Y"
  - index_query → retrieve top-K chunks
  - Efficiently extracts relevant sections from a much larger history

Events collected:
- run_skill_started / run_skill_completed
- skill_node_started / skill_node_completed
- tool_executed (failed operations)

Output: traces_summary.md (summary of success rate, failure patterns, frequent errors)
```

The `plan_improvements` phase references `traces_summary.md` to generate improvement proposals. When `improvement_source: tests`, only existing `run_and_eval` results are referenced (no change).

### Component D — `on_propose` configuration + ask_user approval gate (SMALL)

```yaml
# reyn.yaml
self_improvement:
  on_propose: ask_user   # ask_user | auto | disabled (default: ask_user)
  max_versions: 10
```

| Mode | Behavior |
|---|---|
| `ask_user` | Prompt the user for approval before applying improvements in finalize (default) |
| `auto` | Apply automatically without approval (for trusted environments, CI) |
| `disabled` | Do not apply improvements (dry run only) |

When `on_propose: ask_user`, the `finalize` phase issues an ask_user via InterventionBus (the same mechanism as FP-0005 / FP-0003).

### Component E — `reyn skill rollback` CLI (SMALL)

```
reyn skill rollback <skill_name>           # revert to the previous version
reyn skill rollback <skill_name> --to v2   # revert to a specified version
reyn skill versions <skill_name>           # list version history
```

Example output of `reyn skill versions`:

```
my_skill version history:
  v1  2026-05-01 10:00  (initial save)
  v2  2026-05-05 14:30  improvement: instruction improvement in plan_improvements phase
  v3  2026-05-09 09:15  improvement: failure pattern handling via collect_traces  ← current
```

**Internal rollback implementation:**

```python
# Write the content of .reyn/skill-versions/<name>/v<N>.md
# to reyn/project/<name>/skill.md via write_file
# → Permission check (writing to reyn/project/ requires a Permission declaration)
# → Emit skill_rolled_back event to P6
#   { skill: "my_skill", from_version: 3, to_version: 1, reason: "user rollback" }
```

Rollback itself passes through the Permission model, so attempting to roll back a skill without the required permission results in a PermissionError.

### Meta-improvement (no new implementation required)

Writing to `src/stdlib/skills/skill_improver/skill.md` fails with a PermissionError without a Permission declaration, since `src/` is outside the default write zone. It only works if the user explicitly adds the stdlib path to `permissions.file.write`.

**The Permission model automatically prohibits meta-improvement by default — no additional implementation needed.**

---

## Comparison with Hermes GEPA

| | Hermes GEPA | Reyn (after this FP) |
|---|---|---|
| Who executes improvements | Side effect outside the OS | `skill_improver` skill (within the OS) |
| Improvement trigger | Automatic after 5+ tool calls | User-executed or cron (FP-0001) |
| Permission check | None | write_file op → Permission model |
| User approval | Not possible | Controllable via `on_propose: ask_user` |
| Change record | None | `skill_improved` event in P6 |
| Recovery when broken | Difficult (unclear what changed) | `reyn skill rollback` + P6 tracing |
| Reproducibility | Not guaranteed | Run linked to version via `skill_version_hash` |
| Meta-improvement | Unrestricted | Prohibited by default via Permission model |

---

## Dependencies

- `src/reyn/op_runtime/run_skill.py` — Component A (add `skill_version_hash`)
- `src/reyn/stdlib/skills/skill_improver/` — Components B/C/D (phase extensions)
- `src/reyn/config.py` — Add `SelfImprovementConfig` dataclass
- `src/reyn/cli/skill.py` — Add `rollback` / `versions` subcommands
- `src/reyn/user_intervention.py` / InterventionBus — Component D (ask_user, no changes needed)

Prerequisite PRs: none. Component A (SMALL) can be released independently.
Shares InterventionBus with FP-0005, but the existing ask_user implementation can substitute if FP-0005 is not yet complete.

---

## Cost estimate

**Total: MEDIUM**

| Task | Cost | Notes |
|---|---|---|
| Component A: add `skill_version_hash` event | SMALL | 1 file, 1 change site |
| Component B: `.reyn/skill-versions/` saving | SMALL | Markdown change in finalize phase |
| Component C: create new `collect_traces` phase | MEDIUM | New phase + skill.md graph update |
| Component D: `on_propose` config + ask_user | SMALL | config + branch addition in finalize phase |
| Component E: `reyn skill rollback` CLI | SMALL | 2 CLI subcommands + version list reading |
| Tests (Tier 1 / Tier 2) | SMALL | Contract test for Component A is primary |

The bottleneck is **Component C** (designing the `collect_traces` phase and ensuring the `plan_improvements` phase can effectively leverage `traces_summary.md`).

---

## Related

- `src/reyn/stdlib/skills/skill_improver/` — Existing implementation
- `src/reyn/op_runtime/run_skill.py` — Component A change target
- `src/reyn/events/events.py` — P6 event emit mechanism
- `src/reyn/permissions/permissions.py` — Default write zone definition
- FP-0003 (`0003-budget-exceed-user-approval.md`) — ask_user mechanism (same as Component D)
- FP-0005 (`0005-safety-as-checkpoint.md`) — Shared InterventionBus
- `docs/deep-dives/research/competitive/hermes-agent.md` — Design comparison with GEPA
