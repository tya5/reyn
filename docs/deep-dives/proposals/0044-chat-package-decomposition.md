# FP-0044: `reyn.chat` decomposition — runtime namespace + cluster split + god-file seams

**Status**: proposed
**Proposed**: 2026-06-19
**Author**: e2e-coder session (#312)

## Summary

`src/reyn/chat/` has grown into the agent **runtime** (turn execution, session
lifecycle, multi-agent routing/transport, planning) — not "chat UI". It is ~29
top-level modules + `services/` (21 collaborators), ~18.2k LOC, with two
god-files: `session.py` (5214 LOC, 7 classes incl. the `Session` god-class) and
`router_loop.py` (4443 LOC). (`chat/`'s only real subpackage is `services/`; the
former `chat/tui` and `chat/slash` are stale-empty pycache remnants — the real
TUI/slash moved to `interfaces/` in #1700.) This proposes, **proposal-first**
then staged clean-break PRs (no shims, owner policy):

1. **Rename** `reyn.chat` → a runtime-accurate namespace (candidates below).
2. **Cluster split** the flat module list into concern subpackages.
3. **Relocate UI** (`repl`/`renderer`) under `interfaces/`
   (the #1700 cli/tui/web/chainlit_app grouping left these behind).
4. **Decompose the god-files** (`session.py`, `router_loop.py`) into
   collaborators — continuing the FP-0043 `Session`-slimming (the class FP-0043
   renamed from `ChatSession`) + `Agent` VO extraction.

The seams are settled in this doc **before** any partial extraction, so we don't
cut them wrong.

## Motivation

- **Name drift**: `chat` reads as UI. The package is the agent runtime — turn
  loop, session/agent lifecycle, routing/transport, planning. UI is a small
  minority (`repl.py` 167, `renderer.py` 218).
- **God-files**: `session.py` (5214) and `router_loop.py` (4443) are the two
  largest modules in the package; both have a single ~4000-LOC class. They are
  hard to review, test in isolation, and reason about; FP-0043 began slimming
  `Session` (Agent VO extraction) but the seam was never fully cut.
- **Flat structure**: 29 sibling modules mixing four distinct concerns; no
  subpackage boundary signals which modules collaborate.
- **#1700 inconsistency**: cli/tui/web/chainlit_app were grouped under
  `interfaces/`, but the REPL/renderer UI in `chat/` was left out.

## Concern clusters (current modules → target)

| Cluster | Modules (current `chat/`) | Target |
|---|---|---|
| **(1) session / agent lifecycle** | `session.py`, `registry.py`, `agent.py`, `scoped_session_factory.py`, `agent_locks.py`, `channel_state.py`, `profile.py`, `lifecycle_forwarder.py` | `reyn.runtime.session` |
| **(2) router / turn engine** | `router_loop.py`, `router_tools.py`, `router_system_prompt.py`, `router_op_context.py`, `planner.py`, `reasoning_continuity.py`, `error_format.py`, `reyn_src.py`, **`services/`** (21 collaborators) | `reyn.runtime.engine` |
| **(3) multi-agent routing + transport** | `routing.py`, `transport.py`, `topology.py`, `outbox.py`, `message_bus.py`, `a2a_routing.py`, `mcp_routing.py`, `external_routing.py`, `webhook_routing.py`, `forwarder.py` | `reyn.runtime.routing` |
| **(4) UI / REPL** | `repl.py`, `renderer.py` | `reyn.interfaces.repl` |

> **Correction (PR-A flow-trace)**: `error_format.py` is **runtime**, not UI — it
> is router/LLM-failure **classification** (`classify_router_error`; imports
> `reyn.runtime.budget.BudgetExceeded`; called by `session.py`'s router-loop
> except handler). Moving it to `interfaces/` would invert the dependency
> direction (runtime → interfaces). It belongs in cluster (2) and goes to
> `reyn.runtime` in PR-B. The UI cluster is `repl` + `renderer` only.
>
> **Correction (design-review)**: `chat/tui/` and `chat/slash/` are **stale-empty
> remnants** (only `__pycache__`, no tracked files) — the real TUI/slash live at
> `interfaces/{tui,slash}` since #1700. The stale dirs get cleaned (pycache) in PR-A.
>
> **Deferred to a separate FP (lead decision, 2026-06-19)**: the engine
> cluster's `services/{skill_runner, skill_plan_glue, skill_search}` are NOT
> moved by #312. They consolidate into the existing `reyn.skill` package under a
> **separate skill-consolidation FP** (cross-cutting; authored after #312's
> mechanical stages land). #312 leaves them where they are; the
> `reyn.runtime.engine` cluster excludes them.

## (a) Rename candidates

`chat` → one of, in preference order:

1. **`reyn.runtime`** — **DECIDED** (lead, 2026-06-19): merge into the existing
   `reyn.runtime` (which today holds `budget/` + `cron/` + `limits/`). Accurate
   (the agent runtime), sibling of `reyn.core` / `reyn.interfaces`, and budget/
   cron/limits are runtime concerns — one `reyn.runtime` is coherent. Reads as
   "where a turn actually runs".
2. `reyn.engine` — accurate but narrower (implies just the turn loop, not
   lifecycle/routing). Not chosen.
3. `reyn.agent` — collides conceptually with the `Agent` VO (FP-0043) and the
   `agent.py` module; rejected.

## (d) God-file decomposition seams

### `session.py` (5214 LOC, 7 classes)

Non-`Session` classes are already separable — extract first (low-risk, pure
moves into the lifecycle cluster):

- `RouterCapExceeded` (exc), `PendingOpView`, `AgentRequestBus`,
  `ChatInterventionBus`, `ChatMessage` (VO), `_RouterUsageShim` → own modules.

`Session` (the ~4000-LOC god-class) is then slimmed to a **coordinator** by
extracting its method clusters into collaborators (many already exist under
`services/` — `router_loop_driver`, `intervention_handler`, `snapshot_journal`,
`chain_manager`, `compaction_controller`, …; the Session methods that merely
forward to them become thin). Proposed remaining concern-collaborators to
extract from `Session`'s 161 methods:

- **history/context assembly** (already partly in `router_history_buffer`)
- **intervention coordination** (forwards to `intervention_handler`/`_registry`)
- **persistence/journal** (forwards to `snapshot_journal`)
- **turn dispatch** (forwards to `router_loop_driver`)
- **lifecycle** (start/attach/detach/restore) — the coordinator core that stays.

This is the FP-0043 continuation: `Session` ends as a lifecycle coordinator
holding collaborators, not a god-object.

### `router_loop.py` (4443 LOC)

`RouterLoop` already delegates to `services/router_loop_driver`,
`router_history_buffer`, `router_host_adapter`. The remaining seam: the turn
phases (context build → LLM call → control-IR execution → transition) become
explicit collaborator steps. The two Protocols (`RouterLoopCore`,
`RouterLoopHost`) stay as the host contract.

> God-file decomposition is the **highest-value + highest-care** stage — it is
> NOT a mechanical move and needs its own per-PR seam review. Stages 1–3 (below)
> are mechanical and land first to de-risk; god-file extraction is staged after.

## (e) Staged PR plan (clean break, no shims, byte-gate per PR)

Following the #311 clean-break discipline (git mv → atomic importer repoint →
no shim → byte-gate via rename-detection + 3-ref-class straggler grep):

- **PR-A (UI relocate)**: `repl`/`renderer` → `reyn.interfaces.repl` (+ clean the
  stale `chat/tui`/`chat/slash` pycache remnants). Small, isolated, byte-identical
  move + repoint. (Closes the #1700 inconsistency; lowest risk, lands first.
  `error_format` stays — it's runtime, see the cluster-table correction.)
- **PR-B (namespace rename + cluster split, FOLDED)** — *recommended fold* (was
  PR-B rename + PR-C split). `reyn.chat` → `reyn.runtime.{session,engine,routing}`
  in **one** move + atomic repoint. Rationale: the repoint surface is large
  (`reyn.chat.*` is imported far more widely than `reyn.api`); a separate rename
  then split would repoint **every** importer twice (`chat`→`runtime`, then
  `runtime.X`→`runtime.session.X`). Folding does it once. Byte-gate still holds
  via git rename-detection (each module is a single rename to its final path) +
  the 3-ref-class straggler grep. (If review prefers smaller PRs, the unfold is
  rename-first then split — but pays the double repoint.)
- **PR-C… (god-file decomposition)**: extract `session.py` non-Session classes
  (C1), then `Session` method-clusters into collaborators (C2…), then
  `router_loop.py` turn-phase collaborators (C3…). Each PR = one seam, behavior-
  preserving, with collaborator unit tests. **Per-PR seam review** (not a single
  mega-PR) — this is where wrong cuts cost the most.

Each stage independently byte-gate-able; docs prose (path references) → a
docs-maintainer follow-up after each path-changing stage lands (per #311).

## Cost estimate

**Total: HIGH** (≈ the #311 playbook × N stages + the god-file extraction).
PR-A (UI) + PR-B (rename+split, folded) are mechanical-but-broad (the repoint
surface is larger than #311 — `reyn.chat` is imported far more widely). PR-C+
(god-file) is the genuinely hard, high-value design work. Recommend landing
A→B (de-risk, mechanical) before committing to the C-series schedule.

## Risks / open questions (for design-review)

- **Repoint surface**: `reyn.chat.*` is imported across interfaces, runtime,
  stdlib skills, tests — a 3-ref-class branch-wide grep sizing is needed before
  PR-B (will produce in the PR-B flow-trace).
- ~~`reyn.runtime` merge vs fresh name~~ — **DECIDED**: merge into `reyn.runtime`.
- ~~`services/skill_*` → `reyn.skill`~~ — **DECIDED**: deferred to a separate
  skill-consolidation FP (out of #312).
- **Cluster boundaries**: `reyn_src.py`, `planner.py` placement (engine vs a
  `planning` sub-cluster?) — confirm in review.
- **God-file seam order**: C-series sequencing + whether `Session` slimming can
  proceed independently of the PR-B cluster split or must follow it.

## Related

- FP-0043 (`Session`-slimming — the class renamed from `ChatSession` — + Agent
  VO extraction; the god-file decomposition here continues it)
- #311 / #1783 (`reyn.api` relocate — the clean-break, no-shim, byte-gate
  playbook these stages follow)
- #1700 (cli/tui/web/chainlit_app → `interfaces/` — the grouping PR-A completes)
