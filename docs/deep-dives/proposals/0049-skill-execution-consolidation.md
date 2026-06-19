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

- `skill_runtime.py` → `reyn.runtime.budget.BudgetTracker` (module-level import,
  but **annotation-only** — used solely in type hints, and `from __future__
  import annotations` makes them lazy; it never calls a `BudgetTracker` method,
  only holds + forwards). So this is **TYPE_CHECKING-gatable**: moving the
  import under `if TYPE_CHECKING:` makes `skill_runtime` runtime-independent with
  **no inversion** — the cheapest path (lead-confirmed; docs-maintainer audit
  corrected the original "(fn-local)" note). This makes S2 the lowest-risk cut.
- `skill_runner.py` → `reyn.runtime.outbox.OutboxMessage` (module-level, used as
  the *type* of a DI'd `put_outbox` callback) — plus a few function-local
  runtime deps (forwarder, budget-format). This is the genuine inversion case.

So: `skill_runtime`'s edge is **annotation-only → TYPE_CHECKING-gate** (trivial);
`skill_runner`'s `OutboxMessage` is the one that needs a real decision — see
"Dependency-inversion options". The invariant in both: the resulting
`reyn.skill` must have **zero** `reyn.runtime` imports (asserted per stage).

## Per-module disposition (seam map)

| Module (LOC) | Today | reyn.runtime dep? | Disposition |
|---|---|---|---|
| `core/op_runtime/skill_resolve.py` (93) | op_runtime (Control IR op **backend**) | none (deps `reyn.skill.skill_paths`) | **op-backend group (S4), NOT a clean leaf** — correction (verified at S1): `skill_resolve.py:93 register("skill_resolve", handle)` makes it a Control IR op handler (side-effect-imported by `op_runtime/__init__` alongside run_skill/mcp). Same treatment as `run_skill`: the **op registration stays in `op_runtime`**, the **logic (`_categorize_source` …) delegates to `reyn.skill`**. |
| `runtime/services/skill_search.py` (114) | runtime/services | none (BM25, pure) | **→ `reyn.skill`** — clean, runtime-independent |
| `core/op_runtime/run_skill.py` (270) | op_runtime (Control IR op **backend**) | none at module level | **stays an op backend** in `op_runtime`; **delegates** its skill-running logic to `reyn.skill`. It is registered `"run_skill": RunSkillIROp` in `op_runtime/registry.py` (op-kind → model → purity → backend). Moving the op dispatch out of `op_runtime` would split the Control IR registry from its backends — keep the thin op, move the logic. |
| `skill_runtime.py` (386, `SkillRuntime`, **22 consumers**) | top-level | `reyn.runtime.budget.BudgetTracker` (module-level, **annotation-only**) | **→ `reyn.skill`** — the budget edge is annotation-only → **TYPE_CHECKING-gate** (no inversion). 22 importers repoint. **Lowest-risk substantive cut (S2).** |
| `runtime/services/skill_runner.py` (919) | runtime/services | `reyn.runtime.outbox.OutboxMessage` (type) + fn-local forwarder/budget-format | **→ `reyn.skill`** — the largest cut. `OutboxMessage` needs a real decision in **S3** on the actual diff: it has **~32 importers** (repoint blast radius) and is a Session/presentation VO (a lower `reyn.skill` importing a presentation VO is a layering smell), so (a) relocate-to-`reyn.schemas` vs (b'') skill-local record + a runtime-boundary adapter is decided then, not locked now. |
| `runtime/services/skill_plan_glue.py` (304, `SkillPlanGlue`) | runtime/services | **`reyn.runtime.session` / `chat_message` / `errors`** | **STAYS in `runtime/services`** — refinement: despite being in the dispatch list, the flow-trace shows this is a **Session collaborator** ("skill/plan completion routing + chain timeout *for Session*", extracted from session.py in FP-0019). It is runtime/Session glue, not skill-package logic; moving it would deeply invert (`reyn.skill → reyn.runtime.session`). (Same kind of pre-impl scope refinement as the C6 9→6 / C7 dead-vs-live cuts.) |

**Net (corrected at S1): 3 modules consolidate into `reyn.skill`**
(skill_search, skill_runtime, skill_runner); **`run_skill` AND `skill_resolve`
stay as thin op backends in `op_runtime`, delegating logic to `reyn.skill`**;
**skill_plan_glue stays in `runtime/services`**.

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

- **S1 (clean leaf)**: `skill_search` → `reyn.skill.skill_search`. Pure
  byte-identical move, no inversion, smallest blast radius (one real importer:
  `router_loop.py:29` — distinct from the unrelated `reyn.stdlib.skills.skill_search`
  *skill*). De-risks the pattern + first-wires the layer-direction gate.
  (`skill_resolve` was originally bundled here; the S1 op-caller check found it is
  an op handler → moved to S4. See the disposition table.)
- **S2 (`SkillRuntime`)**: `skill_runtime.py` → `reyn.skill.skill_runtime`, with
  the budget dep **TYPE_CHECKING-gated** (annotation-only — no inversion). 22
  importers repoint. Behavior-preserving. Lowest-risk substantive cut.
- **S3 (`skill_runner`)**: the 919-LOC move. `OutboxMessage` decision (a vs b'')
  made here on the actual diff (~32 importers; presentation-VO layering smell).
  Largest; its own stage + design-confirm + review.
- **S4 (op-backend delegation)**: thin the `run_skill` **and `skill_resolve`** op
  backends to delegate their logic to the consolidated `reyn.skill` entries
  (op registration stays in `op_runtime`; behavior-preserving rewire, not a move).

**Per-stage layer-direction gate (lead's add):** `verify_package_move` checks
stragglers but not import direction, so each stage additionally **asserts
`reyn.skill` has zero `reyn.runtime` imports** — a CI/grep check enumerating all
import forms (`from reyn.runtime…`, `import reyn.runtime…`, dotted-literal). This
makes the layer invariant a mechanical gate, not a review nicety. Each stage is
independently byte-gate-able.

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
