# S10: CLI Smoke — Finding

**Batch**: 17 (RAG-extensible OS Phase 1)
**Scenario**: S10 — CLI smoke (LLM-free)
**Date**: 2026-05-10
**Agent**: Sonnet 4.6 sub-agent

---

## Aggregate Verdict: VERIFIED (30/30)

All 10 test cases passed across all 3 independent fresh-state runs.
Case-pass rate: **30/30 (100%)**. Prediction was 90% verified; actual was 100%.

---

## Verdict Table

| Case | Description | R1 | R2 | R3 | Rate |
|------|-------------|----|----|-----|------|
| T1 | `reyn source list` table format | verified | verified | verified | 3/3 |
| T2 | `reyn source list --json` | verified | verified | verified | 3/3 |
| T3 | `reyn source list` empty manifest | verified | verified | verified | 3/3 |
| T4 | `reyn source describe notes` | verified | verified | verified | 3/3 |
| T5 | `reyn source describe missing_source` (exit 1) | verified | verified | verified | 3/3 |
| T6 | `reyn source rm trial_a --yes` | verified | verified | verified | 3/3 |
| T7 | `reyn source rm missing_source --yes` (exit 1) | verified | verified | verified | 3/3 |
| T8 | `reyn source rm notes` stdin "n" → abort | verified | verified | verified | 3/3 |
| T9 | `reyn source rm notes` stdin "y" → remove | verified | verified | verified | 3/3 |
| T10 | `reyn source --help` | verified | verified | verified | 3/3 |

**Total: 30/30 verified**

---

## Per-Case Assertion Details

### T1: `reyn source list` (table format)

All assertions verified across 3 runs.

Sample stdout (Run 1):
```
notes                          5 chunks  fake/standard                     2026-05-10T01:15:29.832701+00:00
  User notes
reyn_docs                     10 chunks  fake/standard                     2026-05-10T01:15:29.857174+00:00
  Reyn documentation
trial_a                        3 chunks  fake/standard                     2026-05-10T01:15:29.865698+00:00
  Trial data A
```

Assertions:
- exit_0: true
- has_notes / has_reyn_docs / has_trial_a: true
- has_chunk_counts (5, 10, 3): true
- has_embedding_model (fake/standard): true

Table format: `name<24> chunk_count embedding_model<32> last_indexed` with description on next line.

### T2: `reyn source list --json`

Valid JSON with all 3 sources at top-level keys. Each entry contains `description`, `path`, `backend`, `chunk_count`, `last_indexed`, `embedding_model`.

Assertions:
- exit_0: true
- json_valid: true
- has_all_3_sources (notes, reyn_docs, trial_a): true

### T3: `reyn source list` (empty manifest)

Sample stdout:
```
No indexed sources. Run:
  reyn run index_docs --source <name> --path <glob> --description <text>
```

Assertions:
- exit_0: true
- has_no_indexed_sources: true
- has_index_docs_hint (`reyn run index_docs`): true

### T4: `reyn source describe notes`

Sample stdout (Run 1):
```
Name:             notes
Description:      User notes
Path:             .reyn/memory/notes.md
Backend:          sqlite
Chunks indexed:   5
Embedding model:  fake/standard
Last indexed:     2026-05-10T01:15:29.832701+00:00
```

All 7 required fields present: Name, Description, Path, Backend, Chunks indexed, Embedding model, Last indexed.

### T5: `reyn source describe missing_source`

Sample stderr: `Source 'missing_source' not found`

- exit_1: true
- "not found" in stderr: true

### T6: `reyn source rm trial_a --yes` (skip confirmation)

**Driver note**: `REYN_INDEX_DROP_AUTO_APPROVE=1` env var required for non-interactive
permission gate bypass. The `--yes` flag only skips the "Continue? [y/N]" user prompt;
the permission gate (`require_index_drop`) also runs and requires either:
(a) `permissions.index_drop: allow` in reyn.yaml, or
(b) `REYN_INDEX_DROP_AUTO_APPROVE=1` env var.

Sample stdout: `Removed: 3 chunks dropped from source 'trial_a'.`

Core assertions (all verified):
- exit_0: true
- db_existed_before: true (`.reyn/index/trial_a/index.db` present before)
- db_gone_after: true (SQLite file removed)
- sources_yaml_no_trial_a: true (`trial_a:` entry absent from sources.yaml)
- list_no_trial_a: true (subsequent `reyn source list` shows 2 sources, not 3)

**Informational** (not counted in verdict):
- events_index_dropped: false — see Bug B17-S10-01 below.

### T7: `reyn source rm missing_source --yes`

Sample stderr: `Source 'missing_source' not found`

- exit_1: true
- "not found" message: true

Note: "not found" check happens before permission gate, so no auto_approve needed.

### T8: `reyn source rm notes` (no -y, stdin "n")

User prompt fires before permission gate. Sending "n\n" via stdin aborts at the
"Continue? [y/N]" step.

Sample stdout: `This will permanently delete source 'notes' (5 chunks).\nContinue? [y/N]: Aborted.`

- exit_1: true
- notes DB still exists: true (no removal occurred)

### T9: `reyn source rm notes` (no -y, stdin "y")

`REYN_INDEX_DROP_AUTO_APPROVE=1` used; user prompt receives "y\n" from stdin.

Sample stdout: `This will permanently delete source 'notes' (5 chunks).\nContinue? [y/N]: Removed: 5 chunks dropped from source 'notes'.`

- exit_0: true
- notes_db_gone: true
- notes_gone_from_yaml: true (`notes:` entry removed from sources.yaml)

### T10: `reyn source --help`

Sample output:
```
usage: reyn source [-h] <subcommand> ...

Manage indexed sources for the recall tool.

positional arguments:
  <subcommand>
    list        List all indexed sources
    describe    Show source details
    rm          Remove an indexed source (destructive)
```

- exit_0: true
- has_list / has_describe / has_rm: true

---

## Bugs

### B17-S10-01: `index_dropped` audit event not persisted to disk in CLI `rm` path

**Severity**: MED

**Description**: `reyn source rm` creates a bare `EventLog()` (in-memory only)
and attaches no `EventStore` subscriber. The `index_dropped` P6 event is emitted
to the in-memory log (`ctx.events.emit("index_dropped", ...)` in
`src/reyn/op_runtime/index_drop.py:52`) but the event is never written to disk.

The `events/` directory is not created; no `.jsonl` file is produced for CLI `rm`
operations. Audit trail for CLI-initiated index drops is invisible to `reyn events`
replay and external audit tools.

**Root cause**: `cmd_rm` in `src/reyn/cli/commands/source.py:182` instantiates
`EventLog()` without wiring an `EventStore` subscriber. Compare: chat/skill-run
paths wire `EventStore` as a subscriber at session startup.

**Observed**: events_index_dropped_informational=false across all 30 case-runs.

**Classification**: bug fix (restoring documented P6 design — "every state change
emits an event" — for the CLI path).

**Fix direction**: In `_cmd_rm_async`, after creating `events = EventLog()`, create
an `EventStore` pointing at `workspace_root / "events" / "cli_source_rm"` and wire
it as a subscriber via `events.add_subscriber(store.write)`. The `reyn events`
subcommand can then replay CLI rm operations alongside skill-run events.

### B17-S10-02: `--yes` alone insufficient for non-interactive `rm` (setup observation)

**Severity**: LOW (expected behavior, not a bug — informational)

**Description**: `reyn source rm --yes` bypasses the "Continue? [y/N]" user
prompt but NOT the permission gate. In a non-tty subprocess (e.g., CI, pipe,
programmatic invocation), `sys.stdin.isatty()` returns False, causing the
permission resolver to set `interactive=False`. With no config-level
`permissions.index_drop: allow` and no `REYN_INDEX_DROP_AUTO_APPROVE=1`, the
gate auto-denies and rm fails with exit 1.

**Observed**: Run 1 with v1 driver (without auto_approve) saw T6 and T9 exit 1
with "Index drop of ... denied by user."

**Classification**: expected behavior (P6 permission gate is correct). The
dogfood helper `write_dogfood_reyn_yaml` should include `index_drop: allow` for
non-interactive test environments, OR use `REYN_INDEX_DROP_AUTO_APPROVE=1`.
No OS code change needed.

---

## Setup Notes (for reproducibility)

The driver used `REYN_INDEX_DROP_AUTO_APPROVE=1` env var for T6 and T9 to
bypass the non-interactive permission gate. T8 (abort path) did not use auto_approve
because abort fires at the "Continue? [y/N]" prompt level — before the permission
gate — so no permission gate involvement.

Sources were seeded via `write_index_directly()` (bypassing index_docs skill) with:
- `notes`: 5 chunks, source_path=`.reyn/memory/notes.md`
- `reyn_docs`: 10 chunks, source_path=`docs/concepts/architecture/architecture.md`
- `trial_a`: 3 chunks, source_path=`trial/data.md`

All 3 sources used `embedding_model: fake/standard` from `FakeEmbeddingProvider`.

---

## S10 Aggregate Verdict

**VERIFIED** — 30/30 case-runs pass all core assertions.

Predicted: 90% verified. Actual: 100%.

CLI argparse / subcommand wiring confirmed working: `list`, `list --json`,
`describe`, `rm --yes`, `rm` (interactive abort + confirm) all behave as specified.

One structural gap found (B17-S10-01): audit events not persisted to disk for CLI rm.
This is a MED-severity gap in the P6 event audit trail for the CLI path.
