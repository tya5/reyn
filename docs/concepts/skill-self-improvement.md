---
type: concept
topic: skill-self-improvement
audience: [human, agent]
---

# Skill Self-Improvement

Reyn skills can improve themselves from execution traces — automatically, with full version archiving and one-command rollback. The entire process runs as a governed skill (`skill_improver`) rather than as a side effect outside the OS, which means every improvement passes through the [permission model](permission-model.md), is subject to user-approval gating, and is linked to the execution history via `skill_version_hash`. Five components landed together as FP-0006 on 2026-05-15.

Unlike Hermes GEPA — which triggers self-improvement as an unrestricted side effect after 5+ tool calls — Reyn's design treats skill improvement as a first-class, operator-governed operation. See [Comparison with Hermes GEPA](#comparison-with-hermes-gepa) below.

## How it fits together

```
skill_improver (stdlib skill)
    │
    ├─ optional: collect_traces phase ──► recall(sources=["events"]) → traces_summary.md
    │       (FP-0006 C — requires FP-0009 events index)
    │
    ├─ run_and_eval / plan_improvements / apply_improvements
    │
    └─ finalize
           ├─ snapshot pre-apply skill.md → .reyn/skill-versions/<name>/v<N>.md  (FP-0006 B)
           ├─ ask_user gate (config: on_propose)                                  (FP-0006 D)
           └─ apply
              → run_skill_started events carry skill_version_hash                 (FP-0006 A)

Audit + recovery:
    reyn skill versions <name>   list saved versions      (FP-0006 E)
    reyn skill rollback <name>   restore previous version (FP-0006 E)
    → emits skill_rolled_back P6 event                    (FP-0006 E + follow-up)
```

The `collect_traces` phase is optional — it depends on [Operational Intelligence](operational-intelligence.md) (FP-0009) having indexed the events log. When the index is absent, `skill_improver` falls back to running `run_and_eval` directly without a trace-driven context.

## Components at a glance

| Component | What it adds | Source |
|-----------|--------------|--------|
| A | `skill_version_hash` field on every `run_skill_started` event | `src/reyn/op_runtime/run_skill.py` |
| B | `.reyn/skill-versions/<name>/v<N>.md` snapshot + `current` pointer | `skill_improver/version_snapshot.py` + `phases/finalize.md` |
| C | `collect_traces` phase (recall path + raw-events fallback) | `skill_improver/trace_collector.py` + `phases/collect_traces.md` |
| D | `on_propose: ask_user\|auto\|disabled` config + finalize gate | `src/reyn/config.py` `SelfImprovementConfig` + `phases/finalize.md` |
| E | `reyn skill versions / rollback` CLI | `src/reyn/cli/commands/skill.py` |

## Workflow walk-through

A typical self-improvement run for a project skill called `my_skill`:

**1. Invoke `skill_improver`**

```bash
reyn run skill_improver '{"target": "my_skill", "improvement_source": "traces"}'
```

**2. Collect traces**

`skill_improver` calls `recall(sources=["events"], query="my_skill failure patterns")` to retrieve a structured summary of recent runs — phase paths, error types, cost, pass rates grouped by `skill_version_hash`. The result lands in the workspace as `traces_summary.md`.

**3. Plan and apply improvements**

`plan_improvements` drafts concrete changes to `my_skill/skill.md` (instructions, phase graph, or eval criteria). `apply_improvements` writes the revised file via a `write_file` Control IR op — gated by the permission model like any other write.

**4. Run eval**

`run_and_eval` runs `my_skill` against its eval set and computes a pass-rate score. If the score is below the acceptance threshold configured in `skill_improver`'s eval criteria, `apply_improvements` retries up to the configured iteration limit.

**5. Finalize — version snapshot + user gate**

On approval threshold reached, `finalize`:

- Reads the current `my_skill/skill.md` and writes it to `.reyn/skill-versions/my_skill/v2.md`.
- Updates the `current` pointer file to `"3"` (the new version number after apply).
- If `on_propose: ask_user` (default), issues an `ask_user` intervention:

  ```
  Apply v3 to my_skill? (eval score: 0.85 → 0.92)
  [Apply] [Discard]
  ```

- On approval, writes the improved `skill.md` back to `reyn/project/my_skill/`.

**6. Version hash on next run**

The next invocation of `my_skill` emits a `run_skill_started` event with `skill_version_hash` set to the sha256 of the new `skill.md`. `reyn eval compare` can now group runs by hash to detect regressions automatically.

**7. Rollback if needed**

```bash
reyn skill versions my_skill
#   v1  2026-05-01  (initial save)
#   v2  2026-05-05  improvement: instruction improvement in plan_improvements phase
#   v3  2026-05-09  improvement: failure pattern handling via collect_traces  ← current

reyn skill rollback my_skill --to v2
```

Rollback writes the archived `v2.md` back to `reyn/project/my_skill/skill.md` via a `write_file` op (permission-checked), then emits a `skill_rolled_back` P6 event:

```json
{"skill": "my_skill", "from_version": 3, "to_version": 2, "reason": "user rollback"}
```

## Configuration (`reyn.yaml`)

```yaml
self_improvement:
  on_propose: ask_user   # ask_user | auto | disabled (default: ask_user)
  max_versions: 10       # cap on saved versions per skill (default: 10)
```

| Mode | Behaviour |
|------|-----------|
| `ask_user` | Default. `finalize` pauses and shows the improvement diff + eval delta. The user approves or discards before any change lands. |
| `auto` | `finalize` applies without prompting. Intended for CI pipelines or scheduled batch runs where operator trust is established. |
| `disabled` | `skill_improver` runs through all phases and emits the proposed diff as an artifact, but never writes back to the skill. Dry-run mode. |

When `max_versions` is reached, `finalize` deletes the oldest saved version (`v1`) before writing the new snapshot.

## Permission model integration

The permission model handles meta-improvement and stdlib protection without any special-case logic:

**Meta-improvement is auto-禁止 by default.** `src/reyn/stdlib/` is outside the default write zone. Attempting to improve `skill_improver` itself — or any other stdlib skill — results in a `PermissionError` at the `write_file` op dispatch stage, with no special check required in the OS layer (P7 compliant).

**Stdlib rollback is refused by the CLI itself.** `reyn skill rollback` only operates on `reyn/project/` and `reyn/local/` skills. Stdlib skills (`src/reyn/stdlib/skills/`) are ship-bundled and immutable. Users who want to customise a stdlib skill should copy it to `reyn/project/<name>/` first — the skill resolution order (`reyn/project/` > `reyn/local/` > `src/reyn/stdlib/skills/`) ensures the project copy takes precedence.

**`on_propose: auto` requires operator trust.** The default `ask_user` mode is appropriate for interactive use. Switch to `auto` only in environments where the operator has reviewed the improvement pipeline and accepts autonomous writes — for example, a nightly CI job that runs `skill_improver` after evaluating a week of traces.

## Comparison with Hermes GEPA

Hermes' GEPA mechanism triggers improvement as an unrestricted side effect outside the agent runtime. Reyn's approach treats improvement as a governed skill execution.

| | Hermes GEPA | Reyn `skill_improver` |
|---|---|---|
| Execution model | Side effect outside the OS | Stdlib skill — governed by OS runtime |
| Trigger | Automatic after 5+ tool calls | User-invoked or cron (FP-0001) |
| Permission check | None | `write_file` op → Permission model |
| User approval | Not possible | `on_propose: ask_user\|auto\|disabled` |
| Change record | None | `skill_improved` event in P6 audit log |
| Recovery | Difficult (no change record) | `reyn skill rollback` + P6 event trace |
| Reproducibility | Not guaranteed | Every run linked to version via `skill_version_hash` |
| Meta-improvement | Unrestricted | Prohibited by default via Permission model |

For the full Hermes GEPA analysis see [`docs/deep-dives/research/competitive/hermes-agent.md`](../deep-dives/research/competitive/hermes-agent.md).

## See also

- [FP-0006: Skill Self-Improvement](../deep-dives/proposals/0006-skill-self-improvement.md) — full design rationale and component implementation notes
- [Reference: `reyn skill versions / rollback`](../reference/cli/skill.md) — CLI reference
- [Reference: Events — `skill_rolled_back`](../reference/runtime/events.md) — P6 event schema
- [Concepts: Operational Intelligence](operational-intelligence.md) — events RAG that Component C depends on
- [Concepts: Permission model](permission-model.md) — how the permission model gates meta-improvement
- [Stdlib: `skill_improver`](../reference/stdlib/skill_improver.md) — the skill itself
