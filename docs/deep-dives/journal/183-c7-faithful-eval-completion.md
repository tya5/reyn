# C7 — Faithful in-container SWE-bench eval: completion (internal signal)

**Status**: ✅ Faithful 完遂 — 2026-06-01 (install-fidelity + pipeline plumbing proven).
⚠️ **ATTRIBUTION CORRECTED 2026-06-01** (see "Attribution correction" below): the original
"all 5 MODEL-AXIS / zero structural confound" was **wrong** — a structural confound (a fixed
8 KB control_ir offload cap starving the apply phase of file content, issue #1209) was found
via a later context-adequacy (layer-3) re-check. The verdicts are a **mixed (A) structural +
(B) model-navigation** interplay, not pure model-axis.
**Owner**: e2e-coder (faithful run + structural rule-out) + lead-coder (per-step review,
independent co-sign, canonical sign-off).
**Scope**: FP-0008 benchmark's faithful test-evaluation layer (issue #183), built on the
#1115 comprehensive realignment (agent runs in a prebuilt container, edits `/testbed`).
**Internal signal only** — NOT a published SWE-bench score (see "Framing").

> **Framing (read first).** The PASS-rate here is an **internal OS-stability / dogfood
> signal**, never a leaderboard number. The swe_bench skill's verify loop applies the
> instance's `test_patch` and iterates = test-leakage by SWE-bench's rules. We use it as a
> dogfood signal where the question is *"does the faithful pipeline reach a clean verdict
> without structural confound?"*, not *"what score do we get?"*. Per
> `feedback_swe_passrate_internal_signal_not_published` + `feedback_no_confounded_benchmark_number`.

## What C7 set out to prove

That the reyn `swe_bench` skill, run **faithfully in-container** (`--env-backend=docker`,
official prebuilt instance image, repo at `/testbed`, **zero host install**), reaches a
clean verify verdict — install OK, tests collected, `tests_passed` emitted — with the
verdict's quality bounded only by the model, not by any structural/OS confound. The original
C7 blocker was that the *host-run* skill's verify could not build astropy, producing a broken
self-check and a handicapped patch.

## Result (5 Class A astropy instances, each individually trace-verified)

Runs: `--env-backend=docker`, official prebuilt image, `/testbed`, zero host install, model
`gemini-2.5-flash-lite` (free, via litellm :4000). Verdict read from the skill's own
`swe_bench_result.tests_passed` artifact (ground truth); failure-mode from the events trace.
Every verdict was **independently co-signed by lead-coder** via their own events parse (not
a summary hand-off).

> ⚠️ The "failure-mode" column below was written under the original (incorrect) pure-model-axis
> reading. See **Attribution correction** — the failures are a mixed (A) 8 KB-offload structural
> confound + (B) model-navigation interplay, not pure model cognition.

| instance | reached verify→report? | tests_passed | patch | verdict failure-mode (revised: see Attribution correction) |
|----------|:----------------------:|:------------:|-------|--------------------------------------------------|
| astropy-13453 | ✅ | false | 5065 B | edit applied but introduced an `IndentationError` in `html.py` (bad edit). pytest ran in testbed. |
| astropy-13236 | ✅ | false | 0 B | all 6 `file op=edit` on `table.py` returned `status=error` (old_string no-match → file never written) → empty diff. OS reported each error faithfully; 13453 capture worked → not plumbing. base-commit checkout only in setup → not a revert. |
| astropy-13398 | ✅ | false | 4415 B | real ITRS-transform fix attempt, but the model placed `from ...builtin_frames.itrs import ITRS` / `from ...earth import EarthLocation` at **module level** → circular `ImportError` at collection (11 edits ok / 3 error). |
| astropy-13977 | ✅ | false | 960 B | real `quantity.py` fix; verify applied test_patch (`git apply` rc=0), **re-applied** it (rc=1 = already-applied), misread the idempotency failure as "patch failed", reverted, and **never ran pytest**. Apply mechanism proven sound (see rule-out). → enhancement #1206. |
| astropy-14096 | ❌ (abort at apply) | n/a | n/a | `LLM returned invalid JSON after repair and retry` (9 edit errors + unparseable output) → reyn aborted before verify. OS attempted repair+retry then correctly aborted — model-axis, not OS. |

**Summary:**
- **4/5 reached verify→report** (faithful pipeline structurally clean). 1/5 (14096) aborted at
  apply on malformed LLM JSON — model-axis, not an OS bug.
- **install-fidelity** (pytest runs in the testbed env, no "No module named pytest" on any
  instance) + **env-exec** (login-shell) + **verify→report reach** + **patch capture**
  (13453/13398/13977 produced real diffs) all **PROVEN**. These remain solid.
- ⚠️ **Attribution: NOT "all 5 model-axis / zero structural confound" (corrected — see below).**
  A structural confound was found: the apply phase was starved of file content by a fixed 8 KB
  control_ir offload cap (#1209). The verdicts are a **mixed (A) structural + (B) model-navigation**
  interplay (severity scales with file size / offload aggressiveness), not pure model cognition.
- **Internal-signal PASS-rate = 0/5** — but **NOT cleanly weak-model-bound**: the 8 KB-offload
  confound impaired the apply phase in ≥3/5, so a fix (#1209) is expected to raise PASS, not only
  a stronger model. Still NOT a leaderboard score.

## Structural rule-out (the discipline that made the result trustworthy)

Each non-PASS was structurally ruled out **before** attributing it to the model — the core
discipline of this arc (`feedback_structural_verify_before_attribution_default`,
`feedback_cosign_verify_exonerating_evidence_provenance`). Two cases are worth recording:

- **13236 empty patch** — could have been a plumbing bug (edits made but not captured). Primary
  evidence: all 6 edit ops returned `status=error` ("old_string not found"); `workspace_updated`
  touched only `.reyn/swe_bench_test.patch` (table.py never written); base-commit checkout only in
  setup. lead-coder independently parsed the events and matched all points → **model-axis (weak
  model hallucinated old_strings), not plumbing.**
- **13977 test_patch-apply** — the `failure_summary` ("repository lacks the necessary blob to
  perform 3-way merge…") *looked* like a structural apply failure. A **fresh-container experiment**
  refuted it: plain `git apply --check` **and** the reyn `--3way --recount --whitespace=fix --check`
  both succeed on this test_patch at base (the "lacks blob" line is a `--3way` warning that falls
  back to direct and applies). The run's failure was purely the model re-applying an already-applied
  patch and misreading rc=1. → workflow error, plus skill-robustness enhancement #1206.

> ⚠️ **The 13236 rule-out above stopped one layer too early.** It cleared structure (plumbing)
> and outcome (edit `status=error`) but NOT *context-adequacy* — see the correction below.

## Attribution correction (2026-06-01, context-adequacy / layer-3 re-check)

The original conclusion ("all 5 model-axis, zero structural confound") was reached after
clearing two verification layers — pipeline structure and event-level outcome — but **not a
third: context-adequacy** (did the model receive a fair context?). Per the user-direction pin
`feedback_context_adequacy_before_model_axis_attribution`, re-reading the `context_built`
frames the apply model actually decided from refuted the pure-model-axis call.

**Root confound (#1209):** `context_builder.py:33` sets a **fixed 8 KB** control_ir inline cap.
Any `file.read` result over 8 KB has its `content` offloaded to a handle and dropped from the
next decide frame. So in the `apply` phase the model reads a source file, the OS offloads the
content, and on the *next* turn it emits `file.edit old_string=…` for a file it can no longer
see — and fabricates plausible-but-nonexistent `old_string`s from training recall.

**Per-edit primary evidence** (apply phase; `in_preview` = old_string visible in the decide
frame; `ok/err` = edit applied vs `status=error`):

| inst | edits ok/err | old_string in_preview | note |
|------|:------------:|:---------------------:|------|
| 13236 | 0/6 | 0 | table.py = 150 KB; all 6 old_strings absent from the file entirely = hallucination (confirmed against the offloaded content) |
| 14096 | 0/9 | 0 | 215 content-offloads; fully starved → invalid-JSON abort downstream |
| 13453 | 9/12 | 3 | mixed — 9 edits landed (context sometimes adequate), 12 blind failures |
| 13977 | 1/4 | 0 | mixed — 4 blind failures + verify idempotency gap (#1206) |
| 13398 | 11/3 | 2 | mostly adequate (11 edits landed) — closest to genuine model judgment (circular import) |

**The true attribution is a mixed interplay**, ratio varying per instance:
- **(A) structural confound** — the 8 KB offload makes the full file un-inline, so the model
  cannot copy real `old_string`s. Dominant in 13236 / 14096 (large files, heavy offload).
- **(B) model-navigation gap** — instead of recovering the content (`read_offloaded`, or reading
  a targeted region), the weak model looped read→offload→hallucinate. A recovery affordance that
  a stronger model would use exists but went unused.

Neither "pure model-axis" (original error) nor "pure context bug" (the opposite over-correction)
is right. The fix needs **both axes**: (A) window-derive the 8 KB cap (root-fix, same class as
#1201/#1172) + (B) make the offload-recovery affordance effective (discoverable `read_offloaded`
/ apply skill guidance to read targeted regions). Tracked: **#1209**.

**Measurement honesty (pre-conclusion checklist):** the cross-instance "not in file =
hallucination" count is reliable only where the edited file is confirmed in the offload sample
(13236 / 14096); for 13398's small inline files it over-counts (11 edits succeeded → those
old_strings were real). The robust cross-instance signals are `in_preview` + edit `ok/err`. The
fully rigorous per-edit `status × old_string × actual-file` join + dogfood-coder's independent
`in_preview=False` corroboration (13453 / 13977) finalize the precise counts; this section
states the established direction, not exact per-edit totals.

## The 5 structural fixes that unblocked faithful agent-driven runs

Each was a confound hiding behind a "model cognition" framing, peeled by mandatory
structural rule-out:

| # | fix | what it unblocked |
|---|-----|-------------------|
| **#1201** | phase `CompactionEngine` budget resolves the model **class** (static-path used the raw class → 128K window instead of real ~1M) | apply-abort confound — control_ir offload fired **184× → 13** after the fix |
| **#1097** | install-fidelity **PROVEN** (model-independent): prebuilt image builds astropy 5.2.dev64 + FAIL_TO_PASS test collected in `/testbed` | the C7 *core* claim |
| **#1202 (③)** | `DockerEnvironmentBackend.run` execs via a **login shell** (`bash -lc 'exec "$@"' reyn-exec <argv>`) so the image's conda `testbed` env activates | `python -m pytest` resolves (was "No module named pytest"); universal across SWE images; P7-clean + argv-safe |
| **#1203 (①)** | swe_bench `verify` graph → **`[report]`** only; verify.md domain-outcome-only (removed hardcoded "plan"/"apply") | the un-satisfiable `verify→apply` edge (apply input=`plan` needs edits/rationale verify can't emit) crashed astropy-13453; P1/P8 root-fix |
| **#1205** | `swe_bench_runner.extract_patch` uses `json.JSONDecoder().raw_decode` (reyn prints `=== Final Output ===` + indent=2 JSON + trailing token/events lines) | harness can extract the prediction once pipelines succeed (latent; same class as C1/#1063 PR-N13) |

## Caveats (honest)

- **Model**: `gemini-2.5-flash-lite` (weak, free). Absolute PASS is model-bounded; every FAIL
  above is model cognition/workflow, not OS-fidelity.
- **Arch**: amd64-on-arm64 **Rosetta emulation** on this Mac. Install is faithful (official
  image); the architecture is emulated.
- **Not a published score**: internal OS-stability signal only.

## Deferred enhancements (tracked, de-prioritized — do not block C7)

- **#1204** — wire the `verify → plan` re-plan iteration loop (plan.md already accepts
  `verify_state`; small follow-up).
- **#1206** — verify Step 1 should treat an already-applied test_patch (idempotency rc=1) as
  success, not an apply failure (recovers 13977-class "fix landed but model gave up" runs).

## Provenance

Per-instance state dirs: `/tmp/c7_<id>_state/` (events under
`events/direct/skill_runs/2026-06/`, artifacts under `artifacts/swe_bench/<phase>/`). All
verdicts read from `swe_bench_result.tests_passed`; all rule-outs from the events trace,
captured via file→Read (`feedback_verify_via_read_when_terminal_corrupt`). Methodology lessons
this arc: `feedback_not_my_diff_vs_main_broken`,
`feedback_structural_verify_before_attribution_default`,
`feedback_cosign_verify_exonerating_evidence_provenance`.
