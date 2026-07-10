---
type: concept
topic: architecture
audience: [human, agent]
---

# Agent engineering — eight lenses

reyn is read through eight engineering lenses. Each lens is a different way to ask "what does this system get right, and where is it still thin?" The same docs are pointed at from multiple lenses; this index is the map. This page mirrors the eight-lens model in `CLAUDE.md`'s Constitution section and [`docs/concepts/architecture/charter.md`](../architecture/charter.md) (the full 8×7 grounded grid, one column per feature family) — read those two for the canonical, currently-grounded version of this model; this page is the narrative walkthrough.

## The picture

```
 User
  │
  ▼
 Chat session ── router loop ──► LLM decides among:
  │                                 Control IR ops (typed, schema-validated)
  │                                 Pipelines (deterministic DSL)
  │                                 Skills (layered-disclosure instructions)
  │                                       │
  │                                       ▼
  │                          permission gate (exclude → permission → dispatch)
  │                                       │
  │                                       ▼
  │                                      OS ── executes the op
  │                                       │
  │                    ┌──────────────────┼──────────────────┐
  │                    ▼                  ▼                  ▼
  │               Workspace        WAL (crash-recovery /  P6 audit-event log
  │             (artifacts, SSoT)     time-travel substrate)  (per-run trace)
  ▼
 Operator-visible surfaces (CLI, live audit chips, `reyn events` replay)
```

Every layer has a corresponding engineering lens. The lenses don't partition the system; they overlap on purpose — the same feature can ground more than one lens (see charter.md's dual-facet rule).

## The eight lenses

### 1. [System Design](system-design.md)

The macro shape: how control flow, state, and responsibility are distributed across layers. The current split is LLM-decides / OS-executes / feature-owns-its-domain — no new cross-layer coupling.

### 2. [Tool Contract Design](tool-contract-design.md)

How the LLM acts on the world: every side effect rides a typed, validated envelope (a Control IR op), never an untyped string the LLM free-forms.

### 3. [Retrieval Engineering](retrieval-engineering.md)

Getting the right context into the agent at the right time, deterministically (`recall` + the preprocessor step), not stuffed unconditionally into the prompt. This is one of the constitution's two declared honest thin areas.

### 4. [Reliability Engineering](reliability-engineering.md)

Recovery from failure: schema-validate + re-prompt, bounded loops with graceful force-close, timeout + opt-in provider-retry; any derived state survives WAL truncation.

### 5. [Security](security.md)

Permission-gated and sandbox-scoped: no capability reaches the world without passing the gatekeeper.

### 6. [Evaluation](evaluation.md)

Scoring output against a rubric in-run (`judge_output`: LLM scorer + threshold + `on_fail` policy). This is the constitution's other declared honest thin area.

### 7. [Observability](observability.md)

An audit-event trace sufficient to inspect and reconstruct what happened (the P6 audit log, `reyn events` replay, live audit chips) — kept sharply distinct from the WAL-event (crash-recovery) and hook-event (reactivity trigger) meanings of the same word "event."

### 8. [Product Think](product-think.md)

Predictable, cost-disciplined, legible to the operator: CLI/CUI affordance, cost reporting, and token-cost *reduction* (e.g. zero-token `present`/offload) — distinct from the cross-cutting band's `cost/budget (bounding)` member, which is the hard-cap mechanism, not this lens.

## The cross-cutting band

Three of the eight lenses name a *discipline* whose *universal mechanism* is one of five band members every feature obeys, regardless of lens: **permission** (Security), **audit-events** (Observability), **workspace-SSoT**, **crash-recovery/WAL** (Reliability), **cost/budget bounding** (a hard cap, distinct from Product Think's reporting/reduction facet). See `CLAUDE.md`'s Constitution section for the full band definition — it's the substrate every lens-cell in the charter grid stands on.

## How to read this section

- New to agent engineering generally? Read `CLAUDE.md`'s Constitution section and [charter.md](../architecture/charter.md) first — they're the current, grounded model. Come back here for the narrative per-lens walkthrough.
- Coming from another framework? Skip to the lens you care most about; cross-links will pull you back to the others as needed.
- Doing self-assessment for your own system? The "where it's still thin" passages — especially on Retrieval and Evaluation, the constitution's two honest thin areas — are the candid bits.
- **A note on staleness**: four of these eight lens pages (Retrieval, Reliability, Security, Product Think) were written against the phase-graph skill engine deleted in an earlier engine-deletion arc, and each carries a status banner at the top naming exactly what's stale vs. still current. Tool Contract Design and System Design have already gone through a full rewrite; Evaluation and Observability are newly written against the current model. A full de-drift pass on the remaining four is tracked as a follow-up, one page at a time — mirroring how `charter.md` itself was built family-by-family.

## See also

- `CLAUDE.md` (§ Constitution) — the eight lens pass-lines + cross-cutting band, canonical
- [`docs/concepts/architecture/charter.md`](../architecture/charter.md) — the full 8×7 grid, grounded against `docs/feature-map.md`
