# SP improvements from the OpenClaw/Hermes comparison — verdict (#1791)

**Status:** decided. **Verdict: adopt 3 (A1 task-completion, A2 model-family hygiene, #3 memory-quality) by design judgment; reject 3 (abs-path / non-interactive / parallel) as architecture-redundant; #6 no-change.**

Follow-up to the OpenClaw/Hermes SP comparison (`comparison.md`, #1838), which proposed six additions to Reyn's router system prompt. Each was evaluated; a dogfood A/B on weak-tier `gemini-2.5-flash-lite` (production-default `enumerate-all` scheme, #1657) was used as one input.

## Methodological note (load-bearing — corrects an earlier framing)
The dogfood A/B found no flash-lite benefit for the behavioral proposals (#1 / model-family). An earlier draft of this verdict framed that as "reject". **That framing was wrong**: a measurement on one limited env proves a positive effect *exists*, but **cannot prove a universal negative** — "no +V on flash-lite" ≠ "rejects for all models/envs". A null weak-env A/B *informs* but does not reject a design-sound directive. Adoption is therefore a **design judgment** (sound / low-cost / non-harmful / plausibly-helpful on unmeasured envs); measurement rejects only on **env-independent structural** grounds (the OS already enforces the behavior). See `feedback_measure_negative_cannot_prove_from_limited_env`.

## ADOPT (design judgment)

| # | Item | Placement | Why adopt (sound / low-cost / non-harmful) |
|---|------|-----------|---------------------------------------------|
| A1 | TASK_COMPLETION (anti-fabrication, finish-the-task, honest-blocker) | Behaviour static-core, **all-model**, cached | Universally-correct agent behavior; ~zero cost once cached; reinforcing for capable models, plausibly-helpful for weaker/other models the flash-lite A/B can't surface. Subsumes "keep-going". |
| A2 | model-family hygiene (verify-before-acting, check-dependencies, be-concise) | scheme `slot_in_behaviour`, **non-Claude gated** | Good operational hygiene; gated → zero Claude impact; targets the comparison's non-Claude steering point. abs-path + non-interactive dropped (architecture-redundant); keep-going dropped (→A1). |
| #3 | memory-quality (durable facts only; no PR#/SHA/task-log; declarative) | Behaviour static-core, **memory-tool gated** | Memory hygiene is sound; gating keeps cost off non-memory agents (SP-minimize-compatible). |

Gating is non-harm-tested (Tier 2, `test_sp_gating_1791.py`): A1 always present; A2 absent for Claude; #3 absent without the memory tool.

## REJECT — env-independent structural redundancy (NOT a measurement verdict)
Reyn's **architecture already enforces these structurally** — a design fact independent of any env:
- **abs-path**: `file__` tools resolve paths cwd-relative through the workspace layer (`src/reyn/tools/file.py:291`, 344-348) → the LLM never constructs absolute paths.
- **non-interactive flags**: exec runs `stdin=DEVNULL` / no-TTY (`noop_backend.py:147`) → prompts can't hang.
- **parallel batching**: the native tool-call scheme already batches independent calls (baseline batched 3/3 under enumerate-all).
Adding SP prose to request what the OS already guarantees is the wrong layer (**P3/P5**: OS enforces, SP needn't re-instruct). This is a positive validation of Reyn's design — competitor agents need these directives because they drive raw shell/file ops; Reyn's tool abstraction makes them unnecessary.

## NO-CHANGE
- **#4 injection-scan**: security track (#1822), not an SP signal.
- **#6 date granularity**: already day-granular (`router_host_adapter.py:582`); no sub-day prefix-cache invalidation.

## The flash-lite A/B as a data point (not a universal verdict)
- #2 PARALLEL: baseline batches 3/3 natively under enumerate-all → no headroom (confirms the structural-redundancy reject).
- #1 TASK_COMPLETION: short tasks baseline-robust (no headroom); long tasks empty-response-confounded (10/10 runs, provider flakiness, not completion-behavior). → no flash-lite signal, which (per the methodological note) informs but does not reject the design-sound A1.
- The A/B excluded 3 confounds before reporting (ws state-pollution / stale-scheme-override / empty-flakiness); the scheme-override one *inverted* the #2 reading and flipped #1's failure mode, underscoring measure-under-production-config.

## Implementation
SP change (A1 static-core + A2 model-gated slot + #3 memory-gated) via a single `model_family()` classifier (`model_resolver.py`) → resolved family as a raw fact in `router_loop` layer_ctx → the scheme derives the non-Claude policy → gates `slot_in_behaviour` (fact-vs-policy separation, P3/P5-clean). Replay re-record of the 6 affected router fixtures (per the re-record-only-failing + production-scheme + per-run-isolation discipline). Tier-2 gating-non-harm tests added.
