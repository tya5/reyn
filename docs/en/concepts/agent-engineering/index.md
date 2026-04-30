---
type: concept
topic: architecture
audience: [human, agent]
---

# Agent engineering — seven lenses

reyn read through seven engineering perspectives. Each lens is a different way to ask "what does this system get right, and where is it still thin?" The same docs are pointed at from multiple lenses; this index is the map.

## The picture

```
              ┌──────────────────────────────────────────────────┐
              │                                                  │
              │    User                                          │
              │     │                                            │
              │     ▼                                            │
              │    Agent ── selects a Skill ─────► Skill         │   ← System Design
              │                                     │            │   ← Tool Contract
              │                                     ▼            │
              │             OS ◄──── runtime loop ──┤            │   ← Reliability
              │              │                      ▼            │
              │              │                    Phase          │   ← Retrieval
              │              │             (input + instructions)│       (preprocessor)
              │              │                      │            │
              │              │                      ▼            │
              │              │                  Workspace        │   ← Security
              │              │                                   │       (permissions)
              │              ▼                                   │
              │         ┌────────┐                               │   ← Evaluation
              │         │ Events │ ─────► JSONL replay log       │       Observability
              │         └────────┘                               │
              │                                                  │
              └──────────────────────────────────────────────────┘
                                                                       ← Product Think
                                                                          (CLI, cost, UX)
```

Every layer has a corresponding engineering lens. The lenses don't partition the system; they overlap on purpose.

## The seven lenses

### 1. [System Design](system-design.md)

The macro shape: how control flow, state, and responsibility are distributed across layers. In reyn this is the **Phase / Skill / OS** split — Phases are stateless and reusable, Skills own structure, the OS owns execution.

### 2. [Tool Contract Design](tool-contract-design.md)

How the LLM acts on the world: the typed envelope that carries side effects (Control IR), the typed envelope that carries decisions (`candidate_outputs`), and the deterministic enrichment hook (preprocessor).

### 3. [Retrieval Engineering](retrieval-engineering.md)

Getting the right context into the agent at the right time. reyn has `recall_memory` for project- and user-scope facts, integrated as a preprocessor step. This is one of the system's thinner areas — see the page for what's missing.

### 4. [Reliability Engineering](reliability-engineering.md)

Recovery from failure: validation, re-prompt, loop bounds, timeout. reyn validates every LLM output against the next target's schema and re-prompts on rejection; long loops are bounded by `max_phase_visits`. Some pieces (global timeout, richer retry policy, checkpoint/resume) are still on the roadmap.

### 5. [Security](security.md)

Capability gating, sandbox boundaries, trust scoping. The three-layer permission model + AST sandbox for pure Python steps + skill-scoped approvals are the core.

### 6. [Evaluation and Observability](evaluation-and-observability.md)

Knowing whether the agent works, and seeing why. The event log answers "why?"; the eval skill answers "does it?". Both are first-class — the same channel powers debug rendering, replay, and eval analytics.

### 7. [Product Think](product-think.md)

The agent as a product: CLI affordances, cost discipline, predictable UX. Model classes (`light`/`standard`/`strong`), per-run cost reporting, and `output_language` localization are the levers reyn currently exposes.

## How to read this section

- New to agent engineering generally? Read in order — the lenses build a vocabulary.
- Coming from another framework? Skip to the lens you care most about; cross-links will pull you back to the others as needed.
- Doing self-assessment for your own system? The "where it's still thin" passages on retrieval and reliability are the candid bits.

## See also

- [principles.md](../principles.md) — the eight design principles that shape these lenses
- [architecture.md](../architecture.md) — the layered diagram in full
- [phase-vs-skill-vs-os.md](../phase-vs-skill-vs-os.md) — the responsibility split
