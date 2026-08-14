---
type: reference
topic: os-development
audience: [human]
---

# P1–P8 and the code that enforces them

> **Status: P1–P4/P8 removed, #4705.** This page was written against the phase-graph
> skill engine, deleted in #2434's arc. P1–P4 and P8 were invariants specific to that
> engine and have been removed below (not re-described — see each principle's own
> "removed" section for why) rather than kept as false present-tense mechanism prose.
> P5–P7's underlying mechanisms (Workspace as single source of truth, an append-only
> event log, no domain-specific strings in OS code / a single op catalog) remain live;
> their file paths were also refreshed in the same pass (`data/workspace/workspace.py`,
> `core/events/events.py`/`state_log.py`, `OP_KIND_MODEL_MAP` in `schemas/models.py`
> per CLAUDE.md's sync rule) — verified against current source, not assumed.

Each of Reyn's eight OS invariants is enforced by a specific combination of type constraints, compiler checks, and runtime validation — not just by convention. This page maps each STILL-LIVE principle to the exact files and mechanisms that uphold it.

This page focuses on the *how*; the conceptual rationale is in CLAUDE.md.

---

## Quick reference

| Principle | Enforced by | Primary file(s) |
|---|---|---|
| P1 — Phase doesn't know next | *removed with the phase-graph engine, #2434* | — |
| P2 — Workflow owns the graph | *removed with the phase-graph engine, #2434* | — |
| P3 — OS executes | *removed with the phase-graph engine, #2434* | — |
| P4 — LLM picks from candidates | *removed with the phase-graph engine, #2434* | — |
| P5 — Workspace is SSoT | All writes go through `Workspace` with permission gate | `data/workspace/workspace.py`, `op_runtime/file.py` |
| P6 — Events are audit truth | `EventLog` is append-only; state recovery reads events | `core/events/events.py`, `core/events/state_log.py` |
| P7 — OS is domain-agnostic | `OP_KIND_MODEL_MAP` is the only op catalogue; no domain-specific strings in OS code | `schemas/models.py` |
| P8 — Instructions don't list fields | *removed with the phase-graph engine, #2434* | — |

---

## P1–P4 — removed with the phase-graph engine (#2434)

The original P1 ("Phase declares only input_schema and instructions"), P2
("Workflow declares graph and final_output"), P3 ("OS is the runtime
engine"), and P4 ("LLM picks only from OS-provided candidates") were
invariants of the phase-graph skill engine specifically — `Phase`,
`OSRuntime`, `SkillGraph`, `ContextFrame.candidate_outputs`, and every
file this section used to cite (`kernel/runtime.py`,
`compiler/linter.py`, `compiler/expander.py`,
`context_builder.py::build_frame()`/`_build_candidates()`). That engine
was deleted in #2434's arc, with two later cleanup passes (#2772, #3355)
removing the vestigial surface it left behind. No modern 1:1 equivalent
is documented here — CLAUDE.md's own eight-lens framework (System
Design / Tool Contract / Retrieval / Reliability / Security / Evaluation
/ Observability / Product Think) is the CURRENT principle set this page
does not attempt to re-map. This paragraph is the single record of that
fact; do not re-add per-principle "how it's enforced" prose for P1–P4
describing dead code — see #4705 for why (duplicated dead-engine prose
across doc files was the finding that closed this section).

---

## P5 — Workspace is the single source of truth

**What it means**: All durable data an agent's run produces lives in the workspace. Reads and writes go only through Control IR ops, which are gated by the permission system — never an in-process value passed directly between steps.

**How it's enforced**:

`data/workspace/workspace.py` — all reads and writes go through `Workspace.read_artifact()` / `write_artifact()` / `write_file()`. There is no in-memory dict shared across the run.

`core/op_runtime/file.py` — the `file` op handler calls `Workspace.write_file()`, which calls `PermissionResolver.check()` before touching the filesystem. Writes that aren't declared in `permissions.file_write` are rejected.

`core/events/events.py` — `Workspace.write_artifact()` emits a `workspace_updated` event. Any write that doesn't go through Workspace is invisible to the event log and therefore to crash recovery.

---

## P6 — Events are the audit truth

**What it means**: Every state change emits an event. The event log is append-only and replay-capable.

**How it's enforced**:

`core/events/events.py` — `EventLog` exposes only `emit()` (appends) and `to_list()` / `to_json()` (reads). There is no `delete()`, `update()`, or `truncate()` method.

`core/events/state_log.py` — the WAL is a JSONL file written with monotonically increasing `seq` values. `runtime/services/recovery.py::build_recovery()` reads this file forward to reconstruct state — it never writes backwards.

Every meaningful OS action (an LLM call, an op start/complete, a crash) has a corresponding `emit()` call somewhere in its own subsystem — not centralized in one file the way phase-graph-era `kernel/runtime.py` used to do it, since that module (and the single-call-site phase lifecycle it tracked) no longer exists. Missing an emit is still a P6 violation detectable by audit; there's just no one file left to point at for "where every emit lives."

---

## P7 — OS code contains no domain-specific strings

**What it means**: No skill/pipeline name, artifact type, or other domain-specific field name appears as a literal in OS code.

**How it's enforced**:

`schemas/models.py` — `OP_KIND_MODEL_MAP` maps op kind strings (e.g. `"file"`, `"mcp"`) to Pydantic models (relocated here from `core/op_runtime/registry.py` by #1983, so the `Op` union derives from the same map — see `control-ir.md`'s own sync rule). This is the *only* place op kind strings appear in OS code. A new op kind requires adding one entry here, not scattering the string across modules.

`compiler/linter.py` — this linter and `allowed_ops`-in-phase-frontmatter validation were phase-graph-engine-specific and no longer exist (#2434); `ALL_OP_KINDS`/`OP_KIND_MODEL_MAP`'s own role as the single op-kind catalogue is still live, just not enforced through this file anymore — see `docs/reference/runtime/control-ir.md` and `src/reyn/schemas/models.py` for the current source of truth.

**Detection rule**: if a literal naming a specific domain object (a phase name, artifact type, or field name from the removed engine — no current equivalent) appears in OS code — it's a P7 violation.

---

## P8 — removed with the phase-graph engine (#2434)

Original P8 ("Phase instructions don't enumerate artifact fields") depended
on `candidate_outputs`/`ContextFrame`/`phase.instructions`, all specific to
the removed phase-graph engine — same #2434 removal, same "no re-add"
note as P1–P4 above.

---

For adding a new Control IR op kind, see [Add a new Control IR op
kind](add-an-op-kind.md) directly — the walkthrough that used to be
duplicated on this page (with stale phase-graph-era file paths, e.g.
"phase frontmatter" `allowed_ops` validation) has been removed rather
than kept as a second, drifting copy.

---

## See also


- [reference/runtime/control-ir.md](../../reference/runtime/control-ir.md) — op kind catalogue
- [deep-dives/contributing/testing.md](../../deep-dives/contributing/testing.md) — test tier requirements
