# FP-0049: Skill-execution consolidation into `reyn.skill` (#1794)

**Status**: proposed (seam map — design-review only, no cut)
**Proposed**: 2026-06-19
**Author**: e2e-coder session (#1794)
**Gate**: owner-decided (2026-06-18); C-series settle was the precondition. Per
owner, **lead reviews this FP/seam-map before any implementation**
(flow-trace-first). Staged clean-break PRs follow approval. No code is cut here.

> Line numbers as-of `origin/main` post-#1825. Method / section / module names
> are the authoritative anchors.

## Goal

Skill *execution* logic is scattered across three homes — `core/op_runtime`,
`runtime/services`, and a loose top-level module — while `reyn.skill` already
holds the skill *package* concerns (registry, validator, paths, resume,
node_runner, sub_skill_runner). Consolidate the execution logic into
`reyn.skill` so there is one home for "how a skill runs", per #1794.

## Flow-trace finding: the layer-direction constraint (the central question)

**`reyn.skill` is currently runtime-independent** — `grep '^from reyn.runtime'
src/reyn/skill/` is empty. It is a strict *lower* layer that `reyn.runtime`
depends on, not vice versa. So the consolidation's load-bearing question is:
**moving execution modules that import `reyn.runtime` into `reyn.skill` would
invert that layer dependency.** Two of the six targets cross the line:

- `skill_runtime.py` → `reyn.runtime.budget` (function-local).
- `skill_runner.py` → `reyn.runtime.outbox.OutboxMessage` (module-level, used as
  the *type* of a DI'd `put_outbox` callback).

These deps must be resolved (not just moved) for the consolidation to keep
`reyn.skill` a clean lower layer — see "Dependency-inversion options" below.

## Per-module disposition (seam map)

| Module (LOC) | Today | reyn.runtime dep? | Disposition |
|---|---|---|---|
| `core/op_runtime/skill_resolve.py` (93) | op_runtime | none (deps `reyn.skill.skill_paths`) | **→ `reyn.skill`** — clean, already skill-facing |
| `runtime/services/skill_search.py` (114) | runtime/services | none (BM25, pure) | **→ `reyn.skill`** — clean, runtime-independent |
| `core/op_runtime/run_skill.py` (270) | op_runtime (Control IR op **backend**) | none at module level | **stays an op backend** in `op_runtime`; **delegates** its skill-running logic to `reyn.skill`. It is registered `"run_skill": RunSkillIROp` in `op_runtime/registry.py` (op-kind → model → purity → backend). Moving the op dispatch out of `op_runtime` would split the Control IR registry from its backends — keep the thin op, move the logic. |
| `skill_runtime.py` (386, `SkillRuntime`, **22 consumers**) | top-level | `reyn.runtime.budget` (fn-local) | **→ `reyn.skill`** — the substantive cut; requires the budget dep be inverted (see options). 22 importers repoint. |
| `runtime/services/skill_runner.py` (919) | runtime/services | `reyn.runtime.outbox.OutboxMessage` (type) | **→ `reyn.skill`** — the largest cut; requires the OutboxMessage-type dep be inverted (see options). |
| `runtime/services/skill_plan_glue.py` (304, `SkillPlanGlue`) | runtime/services | **`reyn.runtime.session` / `chat_message` / `errors`** | **STAYS in `runtime/services`** — refinement: despite being in the dispatch list, the flow-trace shows this is a **Session collaborator** ("skill/plan completion routing + chain timeout *for Session*", extracted from session.py in FP-0019). It is runtime/Session glue, not skill-package logic; moving it would deeply invert (`reyn.skill → reyn.runtime.session`). (Same kind of pre-impl scope refinement as the C6 9→6 / C7 dead-vs-live cuts.) |

**Net: 4 modules consolidate into `reyn.skill`** (skill_resolve, skill_search,
skill_runtime, skill_runner), **run_skill stays a thin op backend delegating to
`reyn.skill`**, **skill_plan_glue stays in `runtime/services`**.

## Dependency-inversion options (for review — the key decision)

For `skill_runtime` (`reyn.runtime.budget`) and `skill_runner`
(`reyn.runtime.outbox.OutboxMessage`):

- **(a) Move the shared types lower** — if `OutboxMessage` / the budget type are
  data-ish VOs, relocate them to a layer both `reyn.skill` and `reyn.runtime`
  can depend on (e.g. `reyn.schemas` / a `reyn.core` types module). Then the
  skill modules import the type from the lower layer, no runtime dep. *Cleanest
  if the types are genuinely shared data.*
- **(b) Abstract via the existing DI** — `put_outbox` is already a callback; type
  it structurally (a `Protocol` or `Callable[..., Awaitable[None]]` without
  importing the concrete `OutboxMessage`) so `reyn.skill` depends on no runtime
  symbol. Budget similarly passed in, not imported.
- **(c) Accept a narrow runtime-interface dep** — least preferred; reintroduces
  a `reyn.skill → reyn.runtime` edge the layer model forbids.

**Lean: (b) for the callback type, (a) for any genuine shared VO.** Confirm
per-module in review; this is the load-bearing decision and is verified
per-stage (the move is only "clean" if the resulting `reyn.skill` has zero
`reyn.runtime` imports).

## run_skill op-vs-package boundary (lead's flagged judgment)

`run_skill.py` is the **execution backend** for the `run_skill` Control IR op
(`op_runtime/registry.py:124 "run_skill": RunSkillIROp`, purity `external`,
alias `invoke_skill`). The Control IR registry and its op backends live together
in `op_runtime` by design (P3/P4 — the OS runs ops). So: **keep `run_skill.py`
as the op backend in `op_runtime`**, and have it **delegate the actual
skill-running to the consolidated `reyn.skill` entry** (a thin op over the
package). This preserves the registry↔backend locality while the *logic* lands
in `reyn.skill`.

## Staged clean-break PR plan (no shim; byte-gate per stage)

Following the #311 / C-series playbook (git mv byte-identical → atomic importer
repoint → no shim → `verify_package_move.py` straggler 0 incl repo-root config →
full CI per stage):

- **S1 (clean leaves)**: `skill_search` + `skill_resolve` → `reyn.skill`. Pure
  moves, no inversion, smallest blast radius — de-risks the pattern. (skill_resolve
  is an op_runtime module but runtime-independent; confirm its op callers in S1.)
- **S2 (`SkillRuntime`)**: `skill_runtime.py` → `reyn.skill.skill_runtime`, with
  the budget dep inverted (option a/b). 22 importers repoint. Behavior-preserving.
- **S3 (`skill_runner`)**: the 919-LOC move, with the OutboxMessage-type dep
  inverted. Largest; its own stage + review.
- **S4 (run_skill delegation)**: thin the `run_skill` op backend to delegate to
  the consolidated `reyn.skill` entry (behavior-preserving rewire, not a move).

Each stage independently byte-gate-able; each verified to keep `reyn.skill`
runtime-import-free (the layer invariant).

## Open questions for lead + owner

1. **Inversion option (a) vs (b)** per module — lean (b) callbacks / (a) shared VOs.
2. **`skill_plan_glue` stays in runtime** — confirm the refinement (it's Session
   glue, not skill logic).
3. **`run_skill` thin-op delegation** — confirm keeping the op backend in
   `op_runtime` (vs moving the op out, which I do not recommend — splits the
   Control IR registry from its backend).
4. **Stage granularity** — S1–S4 as above, or fold S1's two leaves with S2?

## Related

- #1794 (this) · #312 / FP-0044 (the C-series clean-break playbook this follows)
- FP-0024 (skill_search BM25 origin) · FP-0019 (skill_plan_glue ← session.py)
- `feedback_clean_break_no_transition_shim` (no shim; owner policy)
