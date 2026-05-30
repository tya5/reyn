---
type: how-to
topic: reliability
audience: [human]
---

# Production checklist

**Goal:** Confirm a skill is ready for real workloads before you hand it off or deploy it.

Work through each section in order. Each item names the failure mode it prevents.

---

## Crash recovery

- [ ] **`resume_policy.ambiguous_step` is declared explicitly in `skill.md`.**

  The default (`retry`) is safe for idempotent ops but will re-run destructive
  side effects (file writes, external API calls) if a phase is interrupted
  mid-op. Decide consciously:

  ```yaml
  resume_policy:
    ambiguous_step: retry    # retry | skip | discard_skill | prompt
  ```

  If any op in your skill is non-idempotent, `prompt` or `discard_skill`
  prevents silent double-execution on resume.

- [ ] **Interrupted-resume behaviour has been tested manually.**

  Run the skill, kill the process mid-run (`Ctrl-C` or `kill -9`), restart
  `reyn chat`, and confirm it resumes at the right phase. Check that
  completed phases are skipped and that any side effects are not repeated.
  See [Crash recovery and resume](../operations/crash-recovery-and-resume.md) for the full walkthrough.

---

## Permissions

- [ ] **`permissions:` in `skill.md` lists only the capabilities the skill actually uses.**

  Minimum viable example — read-only skill with no shell or MCP:

  ```yaml
  permissions:
    shell: deny
    file.read: allow
    file.write: deny
  ```

  Over-broad grants (`shell: allow` without need, `file.write: allow` for a
  read-only skill) expand the blast radius of a mis-behaving phase. Declare
  the minimum and add capabilities only when `reyn lint` or a run error
  requires it.

- [ ] **Every `mcp` server entry has an `ops` allowlist, not a wildcard.**

  ```yaml
  mcp:
    - server: github
      ops: [read]        # not [read, write] unless the skill writes
  ```

  A server granted `write` access that only needs `read` is a silent
  privilege escalation waiting to happen.

- [ ] **Non-interactive runs (eval, CI) have pre-arranged approvals.**

  `reyn eval` has no prompt. Run the skill interactively once to persist
  approvals to `.reyn/approvals.yaml`, or add a `reyn.local.yaml` with
  project-wide pre-approval for the local machine. Without this, eval will
  silently fail at the first permission gate. See
  [Permissions reference](../../../reference/config/permissions.md#non-interactive-runs-ci-eval).

---

## Budget

- [ ] **A `cost:` block is present in `reyn.yaml` (or `reyn.local.yaml`).**

  Without it, a runaway skill or routing loop has no cap. A conservative
  starting point for most skills:

  ```yaml
  cost:
    per_agent_cost_usd:
      hard_limit: 2.00
      warn_ratio: 0.8
    daily_cost_usd:
      hard_limit: 10.00
  safety:
    loop:
      skill_tokens_per_chain:
        hard_limit: 100000
  ```

  Check `/budget` after the first production run to see whether the limits
  are realistic. See [Budget reference](../../../reference/config/budget.md) for all dimensions.

- [ ] **`safety.loop.max_router_calls_per_turn` is set if the skill can re-invoke the router.**

  The default is `3`. If your skill composes other skills via `run_skill`,
  confirm that limit is appropriate — too low causes premature abort; too high
  allows runaway loops.

---

## Events

- [ ] **After a successful end-to-end run, the event log contains the expected events.**

  ```bash
  reyn event log --run-id <run_id>
  ```

  At minimum, confirm that a `skill_started`, one `phase_completed` per phase,
  and a `skill_finished` event are present. Missing events indicate a phase
  that mutated state without going through the OS ([P6](../../../concepts/principles.md#p6-events-are-the-audit-truth)).

- [ ] **No events have `status: error` that the skill is silently swallowing.**

  A phase that catches all exceptions internally may complete successfully in
  the artifact while leaving `op_failed` events in the log. Read the log, not
  just the final output. See [Debug with events](../operations/debug-with-events.md).

---

## Eval

- [ ] **An `eval.md` exists in the skill directory.**

  A skill without an eval has no repeatable quality signal. Use `eval_builder`
  to generate a starting rubric if you don't have one yet:

  ```
  reyn run eval_builder -- skill_name=my_skill
  ```

  Then run it:

  ```bash
  reyn eval path/to/my_skill/eval.md
  ```

  See [Tutorial: writing an eval](../../getting-started/05-writing-an-eval.md) for
  guidance on what makes a rubric useful.

- [ ] **The rubric has at least one case that fails on weak or empty output.**

  A rubric that passes on placeholder text offers no signal. For each phase
  section, confirm that submitting an empty or off-topic artifact would fail
  at least one criterion. Shape-only criteria (e.g. "the output has two
  fields") should be paired with evidence-bound criteria (e.g. "the summary
  names the topic from the input"). See [eval-builder-rubric.md](eval-builder-rubric.md).

---

## Lint

- [ ] **`reyn lint <skill_name>` passes with no errors.**

  ```bash
  reyn lint my_skill
  ```

  Lint catches: graph references to non-existent phases, `entry` not in
  `graph`, `final_output` pointing to an unknown artifact, and Python
  preprocessor steps not matched by `permissions.python`. Fix all errors.
  Treat warnings as advisory — read them before ignoring.

---

## See also

- [Crash recovery and resume](../operations/crash-recovery-and-resume.md) — WAL mechanics, resume in practice
- [Debug with events](../operations/debug-with-events.md) — reading the JSONL log step by step
- [eval-builder-rubric.md](eval-builder-rubric.md) — what makes a rubric specific and testable
- [Reference: permissions](../../../reference/config/permissions.md) — full permission schema
- [Reference: budget](../../../reference/config/budget.md) — all cost dimensions
- [Tutorial: writing an eval](../../getting-started/05-writing-an-eval.md)
