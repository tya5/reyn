# Batch 37 — Retrospective

> Eighth dogfood batch. **D2-wrapper (= `invoke_action` description
> ACTION ARG SCHEMAS block) verified visible across all 7 workers.**
> **R-WEB-TRUSTED-PYTHON gate unblock structurally confirmed via
> skill_builder 5-phase completion with 0 permission_denied events.**
> Verified rate **26/58 = 44.8%** (B36: 32.8%, +7V mathematical / +1V
> rubric-artifact-corrected). The B35→B37 target of "above 35%" is
> achieved on the mathematical metric; the artifact-corrected metric
> suggests the structural fixes landed cleanly without inflating
> rubric churn.
>
> The headline is **not** the +7V — it is the **N=4 same-batch
> structural observation** that D2-wrapper's scope is hot-list-only,
> and the corollary **hallucination drift** (`source_id` →
> `source_name`) which is direct primary-data evidence that
> per-key synonym normalization is **structurally bankrupt** as a
> long-term defense.

---

## 1. What this batch verified

### Verified — direct primary data

- **D2-wrapper visible across all workers** (7/7). Each worker quoted the
  `ACTION ARG SCHEMAS:` block from `invoke_action`'s description for at
  least one request. Example (W3): `invoke_action` description carries
  `{web__search, file__read, file__list, skill__skill_builder,
  reyn.source__read, agent.peer__researcher}` with canonical keys.
- **R-WEB-TRUSTED-PYTHON gate unblock** (W6): `narr-3` ran
  skill_builder through all 5 phases to `workflow_finished` with **0
  permission_denied events**. `s-fp12-spawn-1` same outcome. B36's 3
  BLOCKED verdicts (`narr-1`, `s-fp11-3`, `s-fp12-completion-1`) are
  no longer BLOCKED — they moved to REFUTED via mcp_search hot-list
  gap (= the gate is no longer the blocker; a separate routing miss
  is).
- **C1 multi-turn stability** (W7): 37/37 turns clean (= 4 batches
  consecutive: B35/B36/B37). 0 G12 Pattern E.
- **W7 wrapper-path canonical confirmation**: 3 `invoke_action` calls
  over the 37-turn window all used canonical keys (vs B36 N=3
  mismatches before the fix). This is the cleanest evidence that
  D2-wrapper works **when the action is in the hot list**.

### Verified — structural with caveats

- **W3 cluster trajectory held**: B28=6V → B30=2V → B32=2V → B33=3V
  → B35=2V → B36=4V → **B37=4V**. The V drop on S2/S4/S8 is
  attributed by W3 to worktree-freshness hot-list seed gap, not OS
  regression.
- **Direct alias (D2-min/D2-full) non-regression**: properties remain
  non-empty across all workers, identical to B36 baseline.

---

## 2. The headline finding — D2-wrapper scope is hot-list-only (= N=4)

The D2-wrapper fix (`561101a`) embeds canonical-key reminders into
`invoke_action`'s description **for hot-list-active actions**. When the
LLM routes via `invoke_action` to an action that is **not** in the
current hot list, the ARS block has no entry for it, and the LLM
falls back to either the description body's hardcoded examples or
plain hallucination.

Cross-worker same-class observations in B37:

| Worker | Scenario | Action | Hot-list at call | LLM `args` key | Canonical |
|---|---|---|---|---|---|
| W2 | S1 | `rag.operation__drop_source` | absent | `source_id` | `source` |
| W4 | S1 | `file__write` | absent | `text` | `content` |
| W4 | S6 | `rag.operation__drop_source` | absent | **`source_name`** | `source` |
| W5 | S3 | `agent.peer__researcher` | absent (cold) | `message` | `request` |

= **N=4 same-batch observations** — by the cross-batch threshold rule
(memory: `feedback_cross_batch_pattern_threshold` — N≥3 same-class
triggers a structural hypothesis), this is a structural gap, not LLM
noise.

### Sub-finding A — hallucination drift (W4 S6)

Between B36 and B37, the LLM's choice for `rag.operation__drop_source`
shifted from `source_id` (B36) to `source_name` (B37). Both are
non-canonical. **The B34 arg-normalize synonym table (= `source_id` →
`source`) covers only the B36 variant; it does not cover `source_name`,
so the permission gate was not reached in B37.**

This is direct primary-data evidence that **per-key synonym
normalization is structurally bankrupt as a long-term defense**: the
LLM can generate unbounded variants (`source_id`, `source_name`,
`source_key`, `id`, `name`, etc.), and synonym normalization is always
playing catch-up. The structural fix is to put the canonical schema in
front of the LLM **before** it picks the key, not to remap after.

This is now logged to memory (= scope expansion of
`feedback_envelope_layer_fix`).

### Sub-finding B — cold-start gap (W5 S3)

W5 found that the `invoke_action` description body contains a
**hardcoded example**: `"use action_name='agent.peer__<agent_name>'
with args={'message': <user_query>}"`. When the peer is not in the
hot list (= cold start), the ARS block has no peer entry, and the
LLM follows the hardcoded `message` example. After the peer
auto-enters the hot list (warm), the ARS block shows `{request}` and
the LLM picks canonical.

= the **fix is the description body example itself** — it should be
data-driven from the actual schema, or removed.

### Structural fix candidates for B38

1. **Expand D2-wrapper scope to all session-visible actions** (=
   `list_actions` registry, not just hot-list). Highest leverage;
   addresses W2 / W4 / W5 / W6 simultaneously.
2. **Dynamically build `invoke_action` description body example** from
   the canonical schema for the relevant action. Addresses W5 S3
   cold-start specifically.
3. **Schema-based: `invoke_action.args` as `oneOf` discriminated by
   `action_name`**. Cleanest but requires gemini-2.5-flash-lite
   compatibility verification.

---

## 3. Other new findings

### F1 — Ghost alias hot-list seed corruption (W1 + W2)

| Worker | Ghost alias | Likely origin |
|---|---|---|
| W1 | `default_api.web__search` | qualified-name corruption in `action_usage.jsonl` |
| W2 | `skill__create_skill` | renamed skill (canonical: `skill__skill_builder`), prior session persistence |

`reyn agent new` does not wipe `action_usage.jsonl` at fresh-worktree
setup, and the hot-list seed loader does not validate that the alias
name maps to an existing action. Result: stale aliases from prior
sessions persist across worktrees, get into the LLM's tool list, get
selected by `invoke_action`, and fail with "Unknown action".

**Fix candidate** (= LOW–MED): validate alias existence at hot-list
seed load time; reject names not in the current action registry.

### F2 — Worktree-freshness V swing (W2 / W3 / W6)

B36 used worktrees that had accumulated usage history from prior
batches; B37 used **fresh** worktrees. Several scenarios (W3 S2, W3
S4, W6 mcp_search) had their `Δ V` driven by the **absence of
usage-history-seeded aliases** in fresh hot-lists, not by OS-layer
regression. The D2-wrapper fix's V-impact is therefore measured
against a noisier baseline than B36's.

This affects measurement methodology: future batches should either
(a) reset action_usage.jsonl deterministically across runs, or (b)
report V swings annotated with hot-list state.

### F3 — `judge_output_direct` postprocessor schema mismatch (W3 S8)

`judge_phase` postprocessor schema validation fails with missing
`criteria_results` / `passed` / `score` fields. No verdict in reply.
Independent of D2-wrapper. Severity: MED.

### F4 — mcp_search hot-list gap (W6)

R-WEB gate is now structurally open, but mcp_search scenarios still
REFUTE because `mcp_search` is absent from the fresh-workspace hot
list. Adding `mcp_search` to `DEFAULT_HOT_LIST_SEED` (= the same
mechanism that seeded `file__list`, `reyn.source__list`,
`skill__index_docs`, `skill__eval`, `file__grep`, `file__glob`)
would fully exercise the R-WEB fix.

---

## 4. The honest trajectory read

B27 0 → B28 12 → B30 10 → B32 11 → B33 12 → B35 17 → B36 19 → **B37
26**.

Two decompositions:

**Mathematical (= raw verdict counts)**:
- D2-wrapper direct visibility + canonical-key adoption on warm path:
  +6V (W7), +4V (W1), +1V (W4), 0V (W3 hold).
- R-WEB gate unblock: +0V V-count but **+3 BLOCKED freed** (= meaningful
  structural unblock not visible in V count).
- Hot-list seed gap (= worktree freshness): -3V across W2 / W3 / W6.
- Net: +7V vs B36.

**Rubric-artifact-corrected (= W7 normalized for the rubric-scope
change)**:
- W7 reports `ΔvsB36=+0V` after correcting for the rubric-scope
  artifact (= B36 had 2 rubric-eligible long_session scenarios, B37
  rubric-eligibility expanded to 7).
- On this metric, B37 = +1V net = matches the structural-fix
  expectation (= 2 fixes, each verified to land, each with
  scope-limited V impact).

Either way, the **structural fixes (D2-wrapper + R-WEB gate)
landed**; the **V trajectory dominant driver is now hot-list
state**, not LLM behavior under canonical schemas.

---

## 5. Process reflection — what worked

- **B37 fix wave was the cleanest yet**: 2 fixes (D2-wrapper +
  R-WEB), each ~50-line patch, each with Tier 2 contract tests, each
  verified via direct primary data in the next batch. No SP changes.
  No "fix iteration after retest". This is the discipline that the
  B33–B36 sequence converged toward.
- **Cross-pattern recognition at aggregate time**: N=4 invoke_action-
  wrapper-path observations were identified the same batch (= not
  4 batches later, as B35 retro warned could happen). The discipline
  installed in B35 (= `feedback_cross_batch_pattern_threshold`) is
  operating correctly.
- **Direct primary-data discipline**: every worker quoted the actual
  LLM-input schema excerpt + actual tool_call args side-by-side
  before drawing any conclusion. Memory
  `feedback_llm_input_schema_observation` is now operational
  practice, not aspirational.

---

## 6. Process reflection — what didn't work

- **Worktree-freshness measurement methodology gap (= F2)**: B37's
  fresh worktrees produced lower V counts on hot-list-seeded
  scenarios than B36's polluted worktrees. The V trajectory
  comparison across batches now has a confound (= worktree
  state). Future batches should either reset deterministically or
  annotate hot-list state per scenario.
- **3 different journal directories** got created by 3 different
  workers (`batch-37`, `batch-37-d2-wrapper-verify`,
  `batch-37-d2-wrapper-canonical-verify`). Worker prompt should
  pin the canonical journal path. Recipe gap.
- **W7 `dogfood/findings/` write path** is non-canonical
  (`dogfood/findings/b37_w7_long_session_v1.md`). Should be under
  the journal dir. Same recipe-gap class as above.

---

## 7. Fix wave priorities for B38+

1. **D2-wrapper scope expansion to all session-visible actions** (=
   the B37 headline structural hypothesis). Highest leverage.
2. **invoke_action description body dynamic example** (= W5 S3
   cold-start gap). Specific to peer delegation; same mechanism for
   other wrappers with hardcoded examples.
3. **DEFAULT_HOT_LIST_SEED expansion**: `mcp_search` (W6),
   `file__write` (W4 S1), `rag.operation__drop_source` (W2 S1 / W4 S6),
   `web__fetch` (W3 S4). Audit fresh-workspace coverage against the
   B37 W4 / W6 misses.
4. **Hot-list seed validation at load** (= F1): reject ghost aliases
   that don't map to current registry.
5. **W3 S8 `judge_output_direct` schema validation** (= F3): MED bug,
   judge_phase postprocessor missing fields.
6. **B27-H4 acompletion-never-awaited** (= #52) still open.

---

## 8. Goal restated

Eight batches in. The B36 retrospective's primary worry — that the
N=4 same-batch wrapper-path observation pointed to a structural gap
the D2 direct-alias fix did not close — is now confirmed. B37's
D2-wrapper fix made the gap **measurable** (= ARS block visible to the
LLM, but hot-list-scoped only), and B37's data identified the **next
structural target** (= scope expansion).

The discipline layer this batch added is **same-batch attribution of V
swings to worktree state vs OS state**. The W3 retrospective
explicitly separated worktree-freshness V swing from OS regression
without me needing to ask. This closes the measurement-confound gap
the B35 H5 ablation hinted at.

Target for B38: D2-wrapper scope expansion (= 1) verifies wrapper-path
arg-canonical on **all session-visible actions**, not just hot-list.
Hallucination drift (W4 S6 evidence) is structurally eliminated via
schema-upfront, not synonym-normalize. Net verified rate above 50%.
