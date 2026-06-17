# 4-way refresh — results (2026-06-17, point-in-time)

weak `gemini-2.5-flash-lite`, N=5/scenario, HEAD `b4158974`. **Internal-signal only** (not a published leaderboard).

## Completion (success/N)
| scheme | S1 | S2 | S3 | M1 | M2 | total |
|---|---|---|---|---|---|---|
| enumerate-all | 5/5 | 3/5 | 5/5 | 4/5 | 5/5 | **22/25** |
| codeact | 5/5 | 4/5 | 4/5 | 2/5 | 5/5 | 20/25 |
| retrieval | 5/5 | 0/5* | 5/5 | 0/5* | 5/5 | 15/25 |
| universal-category | 5/5 | 1/5 | 3/5 | 0/5 | 5/5 | 14/25 |

\*retrieval S2/M1 = timeout-censored (see below), NOT cognition fail.

## Efficiency (mean round-trips, lower=better)
| scheme | S1 | S2 | S3 | M1 | M2 |
|---|---|---|---|---|---|
| enumerate-all | 2.0 | 2.2 | 2.2 | 3.6 | 4.0 |
| universal-category | 2.0 | 2.0 | 2.4 | 4.0 | 4.0 |
| codeact | 2.0 | 2.0 | 3.4 | 4.2 | 3.4 |
| retrieval | 2.0 | 4.8 | 2.0 | 4.4 | 4.0 |

## Structural-defect markers (primary value)
- **#1667 foot-gun (reyn_source/run_code misselection): ZERO / 100 runs** — host-repo → independently confirms #1667 is /testbed-(external-repo)-context-specific.
- codeact: empty-runs 10/25 (provider-intermittent-empty exposure — codeact's larger payloads; known fragility).
- enumerate: 0 timeouts, 0 empties, 0 defects (fastest-terminating).
- retrieval: 9 timeouts (S2/M1 read-heavy — the search→embed-query→represent per-round cost ≈40s × multi-round sequential reads → 200s cap; standalone w/o cap COMPLETES = correct-but-slow, tuning not defect).

## Verdict
- **enumerate-all = weak default, confirmed** (best completion + cleanest/fastest). Owner default-switch holds.
- **retrieval = catalog-scaling opt-in** (clean single-step/chain; slow on read-heavy multi-file). NOT a weak-default replacement.
- **codeact = capable-but-fragile** (provider-empty exposure; composition edge needs a capable model).
- **universal = worst** (invoke_action indirection).

## Method note (measurement-infra)
Driver invocation MUST drain stdout/stderr safely (file-redirect OR Popen+communicate, NOT `subprocess.run(capture_output=True)`): the enumerate arm initially showed all-200s-timeout + rounds=5 — a CAPTURE-PIPE DEADLOCK on enumerate's larger stdout, NOT an enumerate defect (manual run = 9s clean). Re-ran enumerate with the drain-safe method → valid (0 timeouts). The "all-timeout AND mostly-success" internal inconsistency is the tell.
