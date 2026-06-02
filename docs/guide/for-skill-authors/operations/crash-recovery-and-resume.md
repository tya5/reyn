---
type: how-to
topic: reliability
audience: [human]
---

# Crash recovery and resume

Reyn automatically resumes interrupted skills when you restart `reyn chat`. This page explains what that means in practice, how to control it, and when to reset instead.

---

## What happens when a skill is interrupted

Every phase transition and op result is written to a Write-Ahead Log (WAL) at `.reyn/state/wal.jsonl` before it takes effect. If the process crashes — power loss, `kill -9`, OOM, anything — the log survives.

When you restart `reyn chat`, Reyn replays the WAL and fast-forwards to where the skill was:

```
Skill run:  [Phase A ✓] → [Phase B ✓] → [Phase C — crashed here]
Resume:     skip A, skip B, re-enter C from the start
                                         ↑ memoized ops not re-executed
```

**Completed phases** are skipped entirely.
**The interrupted phase** restarts from its beginning, but any ops that had already completed inside it are replayed from the memo cache — the LLM is not called again for them, and side-effect ops that finished are not re-executed.
**External ops** (`web_fetch`, `web_search`) are re-executed on resume because the outside world may have changed.

---

## Resume in practice

No flags needed. Run the same command:

```bash
reyn chat
```

On session start, Reyn calls `_auto_resume_active_skills()`. Any skill that was in-flight is automatically resumed. You'll see a status line like:

```
↺ Resuming: researcher (run_id: abc123) — Phase C
```

The resume happens in the background. You can keep chatting while it completes.

### View in-flight skills

```
/skill list
```

Shows all active skill runs with their current phase and status.

### Discard a run you don't want to resume

```
/skill discard <run_id>
```

Removes the run from the resume queue without deleting the workspace or events log.

---

## Ambiguous steps

Sometimes a phase is interrupted mid-op — the op was started (`step_started` event) but never completed (`step_completed` event). Reyn calls these *ambiguous steps*.

The default policy for ambiguous steps is `retry`: the op runs again from the start. You can override this per-skill in `skill.md`:

```yaml
resume_policy:
  ambiguous_step: retry    # retry (default) | skip | discard_skill | prompt
```

| Policy | Behaviour |
|---|---|
| `retry` | Re-run the op (safe for idempotent ops) |
| `skip` | Mark the op as skipped and continue |
| `discard_skill` | Abandon the run entirely |
| `prompt` | Ask the user what to do before continuing |

---

## Where state is stored

| Path | Contents |
|---|---|
| `.reyn/state/wal.jsonl` | Write-ahead log (survives crashes) |
| `.reyn/agents/<name>/state/snapshot.json` | Latest agent snapshot |
| `.reyn/agents/<name>/state/skills/<run_id>.snapshot.json` | Per-skill resume checkpoint |
| `.reyn/events/<caller>/skill_runs/<YYYY-MM>/…jsonl` | Immutable audit event log |

The audit event log (`.reyn/events/`) is **never deleted** by Reyn — not even by `--reset`. It is the source of truth for what happened ([P6](../../../concepts/architecture/principles.md#p6-events-are-the-audit-truth)).

---

## Opting out of resume

### `--no-restore` — skip resume this session only

```bash
reyn chat --no-restore
```

Reyn starts fresh for this session. Existing state files are **preserved** — you can resume again later by running without the flag.

### `--reset` — delete state and start clean

```bash
reyn chat --reset
```

Reyn asks for confirmation (`yes`), then deletes the WAL and all skill snapshots. Use this when:

- A schema version upgrade broke the existing state.
- You want a guaranteed clean start and don't need to recover the interrupted run.

```
The WAL, agent snapshot, and skill snapshots will be deleted.
The events log (.reyn/events/) will NOT be deleted.
Type "yes" to confirm: _
```

`--reset` does **not** delete workspace artifacts or the events log.

---

## Debugging a failed resume

If a skill fails during resume, check the events log:

```bash
reyn event log --run-id <run_id>
```

Or read the JSONL directly:

```bash
cat .reyn/events/agents/<name>/skill_runs/<YYYY-MM>/<timestamp>_<skill>.jsonl | jq .
```

See [Debug with events](debug-with-events.md) for a step-by-step walkthrough.

---

## Key guarantees

- Completed phases are **never re-executed** on resume.
- LLM calls that finished are **memoized** — they don't cost tokens again.
- Side-effect ops that completed are **not repeated** on resume.
- The events log is **append-only** — nothing Reyn does during resume overwrites past events.

---

## See also

- [concepts/skill-resume](../../../concepts/skills/skill-resume.md) — deeper explanation of the WAL + forward-replay mechanism
- [Forward-replay design](../../../deep-dives/decisions/0002-forward-replay-resume.md) — why forward-replay, not phase-head re-execution
- [Auto-resume design](../../../deep-dives/decisions/0012-auto-resume-default.md) — why auto-resume is the default
- [Debug with events](debug-with-events.md) — reading the JSONL log
