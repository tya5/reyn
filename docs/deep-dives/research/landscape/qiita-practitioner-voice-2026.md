---
title: "AI Agent Practitioner Voice — Qiita Community Analysis 2026-05"
last_updated: 2026-05-10
status: stable
sources:
  - url: https://qiita.com/tai0921/items/04d123bf684e55ce0cd4
  - url: https://qiita.com/y-hirakaw/items/7b714064bffd10c36d06
  - url: https://qiita.com/miruky/items/155f3b5a0dcde72fcd10
  - url: https://qiita.com/nogataka/items/c1d382dab8454d434d7e
  - url: https://qiita.com/kai_kou/items/19033157daccf9ed32cd
  - url: https://qiita.com/nohanaga/items/f974bcc4b1d49702c320
  - url: https://qiita.com/2G_TechBlog/items/295f062f88c0b7b44eb3
  - url: https://qiita.com/ksonoda/items/08bdfadfb760043f2183
  - url: https://qiita.com/emi_ndk/items/4f70389a0fac717df6a9
  - url: https://qiita.com/takahashi_yukou/items/5d030bb43ab3d361b755
  - url: https://qiita.com/ABC-KeisukeKashio/items/687c347279765b735964
  - url: https://qiita.com/Nobuhiro_Okamoto/items/018edfb458b41d5b3aa3
  - url: https://qiita.com/kai_kou/items/9acab428a5c27e442163
  - url: https://qiita.com/keitah/items/654fdf219391e19f2df2
  - url: https://qiita.com/ymaeda_it/items/567bc5adf36f592e8e6e
  - url: https://qiita.com/s977043/items/0a43ef1991769fc07ae8
  - url: https://qiita.com/s977043/items/3ed2cb58d22ac41fe2e1
  - url: https://qiita.com/syukan3/items/9e30b59380b5dc26a134
  - url: https://qiita.com/pythonista0328/items/f7aa01a8ca75a749ed70
  - url: https://qiita.com/ABC-KeisukeKashio/items/26baab6e747e5b536163
---

# AI Agent Practitioner Voice — Qiita Community Analysis 2026-05

This document surveys approximately 20 AI agent-related articles published on Qiita (qiita.com)
from late 2024 to May 2026, recording what Japanese developers are struggling with, what excites
them, and how they frame the space. Qiita is Japan's largest engineer-focused technical blogging
platform, distinct from Zenn in demographics: Qiita skews toward a broader and more tutorial-oriented
readership, with articles published by engineers at SIers (system integrators), mid-size product
companies, Microsoft and Oracle Japan evangelists, security researchers, and individual contributors
at small firms. A notable Advent Calendar culture produces dense practitioner output each December.

Compared to Zenn, Qiita content is more tutorial-heavy and framework-survey-oriented. However, the
practitioner-reflection articles that exist on Qiita surface a different dimension of concern: where
Zenn authors emphasize organizational readiness and regulatory governance, Qiita authors emphasize
**implementation-time control failures** — the harness engineering gap, observability as a missing
prerequisite, supply chain risks in the emerging agent skill ecosystem, and the review-velocity
bottleneck in AI-augmented team development. These are operational and architectural concerns that
arise earlier in the deployment journey than Zenn's enterprise governance framing.

---

## Articles Surveyed

| # | Title / Summary | Author type | Date | Reception |
|---|---|---|---|---|
| 1 | "AI Agents Enter 'Production' — 57% of enterprises face the production wall" — PoC/production gap, legacy integration costs, governance as an afterthought | Individual analyst (@tai0921) | 2026-05 | Analytical |
| 2 | "AI Agent First Year: ideals vs. reality at 2025 year-end" — efficiency myth, maintenance blindness, context overload | Individual practitioner (@y-hirakaw) | 2025-12 | Year-end reflection |
| 3 | "Harness Engineering — the next paradigm after context engineering" — multi-session stability, 4-layer feedback loops, technical debt amplification | Individual engineer (@miruky) | 2026-03 | 99 LGTMs, high signal |
| 4 | "Reading Anthropic's multi-agent research system — lessons from prototype to production" — last-mile engineering, token usage explaining 80% of variance | Individual practitioner (@nogataka) | 2026-02 | 22 LGTMs |
| 5 | "Datadog State of AI Engineering 2026 — why 5% of production LLMs fail" — rate limiting, agent sprawl, prompt caching underutilization | Individual engineer (@kai_kou) | 2026-05 | Data-driven analysis |
| 6 | "Multi-agent controversy and AutoGen: context engineering memo" — Cognition vs. Anthropic vs. LangChain positions, context as the core challenge | Microsoft MVP (@nohanaga) | 2025-07 | 9 LGTMs, synthesis |
| 7 | "2026 approach to AI agent integration with legacy enterprise systems" — API-first, MCP as intermediary, human accountability for final decisions | SI company engineer (@2G_TechBlog) | 2026-01 | Enterprise framing |
| 8 | "2025: AI Agents as a new wave in generative AI" — function-calling errors, execution sequence failures, LLMs as probabilistic not deterministic | Oracle Japan (@ksonoda) | 2025-02 | 439 LGTMs, widely read |
| 9 | "Complete guide to multi-agent orchestration 2026" — MCP + A2A architecture, 86% of copilot spend toward agent systems | Individual (@emi_ndk) | 2026-01 | Tutorial survey |
| 10 | "LLM-based multi-agent systems: foundational concepts" — L,O,M,A,R agent model, graph-based communication, context window limitations | Individual (@takahashi_yukou) | 2025-04 | Foundational article |
| 11 | "What are sub-agents? Multi-agent design patterns and frameworks" — 5 critical pitfalls including token explosion, infinite loops, observability gaps | SI company (@ABC-KeisukeKashio) | 2026-03 | Practical checklist |
| 12 | "Microsoft Ignite 2025: security and governance in the AI agent era" — Agent 365, Entra Agent ID, least privilege, shadow agent detection | Microsoft employee (@Nobuhiro_Okamoto) | 2025-11 | 3 LGTMs, enterprise security |
| 13 | "AI agent skill supply chain risk — 22,511 skills audited" — 34% contain security issues, zero runtime verification in existing registries | Individual security researcher (@kai_kou) | 2026-03 | Security audit findings |
| 14 | "Why traditional AI security won't work in 2026 — new threats in the agent era" — confused deputy problem, memory contamination, cascade failures, infinite loops at $50k/day | Security engineer (@keitah) | 2025-12 | 3 LGTMs, threat taxonomy |
| 15 | "METI AI Guidelines v1.1 requirements checklist — ISO 42001 cross-mapping" — AI Promotion Act (June 2025), 10 principles, compliance gaps | Individual (@ymaeda_it) | 2026-03 | Compliance reference |
| 16 | "What I learned from integrating AI coding agents into team development in 2025" — review velocity bottleneck, PlanGate approval pattern, scope creep | Individual engineer (@s977043) | 2026-04 | Honest field report |
| 17 | "What to decide in advance to make AI-written code reviewable" — Purpose/In scope/Out of scope/Review focus framework | Individual engineer (@s977043) | 2026-04 | 1 LGTM, practical |
| 18 | "Making AI agents observable with AgentOps" — black-box internals, regulatory compliance challenges, cost/performance tradeoffs | Individual (@syukan3) | 2024-12 | 4 LGTMs, tooling |
| 19 | "2026 software development trends: multi-agent collaboration and enterprise acceleration" — role shift from code writers to agent orchestrators, TELUS/Zapier case studies | Individual (@pythonista0328) | 2026-01 | Forward-looking |
| 20 | "Building AI agents for beginners in 2025" — observability from inception, audit logging, least privilege, human approval gates for risky operations | SI company (@ABC-KeisukeKashio) | 2025-11 | Tutorial with governance |

---

## Top 3 Frustrations

### ① The harness gap: agents cannot be trusted to run unsupervised across sessions

The single most distinctive Qiita signal is the articulation of a **structural control gap** that
appears when agents run long-duration or multi-session tasks. This goes beyond general "agents are
unreliable" sentiment — it is a specific engineering diagnosis.

Article 3 (@miruky, 99 LGTMs) names this gap precisely and proposes "harness engineering" as the
response:

> "AIエージェントにコードを書かせたら、半分書いたところでコンテキストが切れて..."
> (You ask an AI agent to write code, and midway through the context cuts out...)

The specific failure modes identified: premature completion declarations, context loss causing
regressions in previously working code, technical anti-patterns spreading exponentially, and
insufficient testing between phases. The author cites a 350,000-line codebase completed in 52 days
as evidence that the pattern is tractable — but only with explicit harness architecture.

Article 5 (@kai_kou) quantifies how far the industry is from solving this: 5% of production LLM
requests fail, 60% due to rate limiting — and 59% of agent requests remain monolithic (single
service calls), suggesting that most deployments have not yet adopted the multi-agent orchestration
patterns that would make the harness problem tractable.

Article 4 (@nogataka, 22 LGTMs) adds that even Anthropic's own multi-agent system "spawned 50+
sub-agents for simple queries" in early versions and required "substantial engineering" for the
prototype-to-production transition.

> "エージェントシステムでは『ラストマイル』がしばしば旅程の大半を占める"
> (In agent systems, the "last mile" often comprises most of the journey)

This frustration has no equivalent in Zenn's discourse, which focuses on organizational and
governance readiness. Qiita practitioners are closer to the implementation surface and hitting
the structural limits of current agent architectures directly.

### ② Observability is absent when it matters most

Across multiple Qiita articles, **observability is identified as a prerequisite that most
deployments are missing** — not a nice-to-have improvement.

Article 11 (@ABC-KeisukeKashio) lists "observability gaps" as one of five critical pitfalls:
"debugging failures becomes impossible without proper tracing infrastructure." The article 18
(@syukan3) frames it as a black-box problem:

> "エージェントの内部プロセスがブラックボックス化しており、予期せぬ動作やエラー"
> (Agent internal processes remain black-boxed, making unexpected behaviors difficult to identify)

Article 20 (@ABC-KeisukeKashio) is explicit that observability must be built from the start:
"logging/tracing/audit should be built in from the start to avoid high retrofitting costs" —
capturing input/output pairs, tool invocations, model versions, and execution costs.

Article 13 (@kai_kou) escalates this from internal observability to external security: a March 2026
audit of 22,511 agent skills found 34% contain security issues, and "hook settings auditing does
not exist across any current registry." The implication is that the observability gap is not only
an operational problem but an active security surface.

The Datadog 2026 data (Article 5) adds that only 28% of organizations utilize prompt caching despite
69% of input tokens residing in system prompts — a finding that indirectly measures how little
systematic cost visibility practitioners currently have. You cannot optimize what you cannot see.

### ③ Review velocity is the real bottleneck in AI-augmented development

A pattern unique to Qiita's 2025-2026 practitioner writing: the **review-velocity problem**.
This is not the oversight-fatigue framing from Zenn's Article 2 (a middle-manager drowning in
agent outputs). It is a more specific engineering observation: AI code generation accelerates
implementation faster than human review pipelines can absorb.

Article 16 (@s977043) identifies the root cause directly:
> "AIが速く書けるからこそ、人間があとから読む差分は大きくなりやすい"
> (Because AI writes quickly, the diffs humans must later review tend to grow larger)

The author's solution — "PlanGate," a mandatory plan-approval step before AI writes a single line
of code — defines four elements: Purpose, In scope, Out of scope, and Review focus. The "Out of
scope" designation proved most effective at controlling diff size.

Article 17 (@s977043) makes the core principle explicit:
> "AIに速く書かせる前に、レビューできる単位に切る"
> (Divide work into reviewable units before asking AI to write quickly)

Article 2 (@y-hirakaw) frames the same problem from the team dimension: "vibe coding" (intuitive,
quick AI-assisted implementation) produces code that the team cannot maintain. The failure mode is
not inaccuracy — it is that **process design has not caught up with generation speed**.

This is a more implementation-grounded version of Zenn's organizational-readiness concern, and it
points to a specific structural solution: plan-gating as a mandatory handoff primitive.

---

## Top 3 Interests / Excitement

### ① Context engineering and harness engineering as emerging design disciplines

The article with the most LGTMs in this survey (Article 3, @miruky, 99 LGTMs) represents genuine
practitioner excitement about **systematizing agent control as an engineering discipline** — not
just a collection of prompting tricks.

Article 6 (@nohanaga) synthesizes three major industry positions on multi-agent context management
(Cognition AI, Anthropic, LangChain) and identifies the consensus:
> "AIの新しいスキルはプロンプティングではなくコンテキストエンジニアリング"
> (The essential new skill is context engineering, not prompting)

This excitement is distinctly Qiita-flavored: it is engineering-methodology enthusiasm — the
recognition that a systematic framework exists for a problem previously treated as unpredictable.
Article 3 provides the five-pillar harness framework (environment initialization, incremental
progress, four-layer feedback loops, codebase-as-context, technical debt control) and the
empirical validation (350K-line project in 52 days).

The emergence of tools like Claude Code and Kiro CLI that embed harness engineering natively
is cited as accelerating this direction — the industry is converging on structured control
over ad-hoc prompting.

### ② Multi-agent architecture as a tractable specialization problem

Qiita practitioners exhibit more concrete enthusiasm about multi-agent architecture than
Zenn authors, likely because Qiita readers are closer to implementation. The framing is not
"can we trust agents?" but "how do we decompose the problem correctly?"

Article 8 (@ksonoda, 439 LGTMs — the highest reception in this survey) is the baseline document:
a clear statement of what makes agents different (autonomous multi-step execution, external system
integration) and why they fail (function-calling selection errors, execution sequence failures,
probabilistic vs. deterministic mismatch). Its 439 LGTMs indicate this framing resonated across
the broad Qiita readership.

Article 4 (@nogataka, 22 LGTMs) adds the empirical result: properly designed multi-agent research
systems show 90.2% performance improvement over single Claude Opus 4, with token usage explaining
80% of performance variance. The excitement is about the tractability: these are optimizable
parameters, not fundamental limits.

Article 11 (@ABC-KeisukeKashio) provides the practical design vocabulary: precision through
specialization, parallel processing, cost optimization through model differentiation. The pitfalls
are enumerated (token explosion, infinite loops, context discontinuity, over-engineering,
observability gaps) — a sign that the community has moved past naive enthusiasm to
considered engineering.

### ③ The security/governance gap as a solvable design problem

Where Zenn's security/governance articles tend toward policy and organizational framing, Qiita's
articles express **genuine engineering excitement about solving the security problem
architecturally**.

Article 14 (@keitah) proposes a Zero Trust architecture for the AI agent era — not as regulatory
compliance but as a technical design challenge. The four new threat categories (confused deputy,
memory contamination, cascade failures, infinite loops) are framed as tractable problems with
known solution patterns.

Article 12 (@Nobuhiro_Okamoto) demonstrates that platform vendors are building the infrastructure:
Microsoft Entra Agent ID, least-privilege controls, shadow agent detection, and real-time anomaly
monitoring. The practitioner excitement here is that the tooling is catching up.

Article 15 (@ymaeda_it) positions Japan's AI Promotion Act (effective June 2025) and METI
guidelines v1.1 not as burdens but as a clarity-providing framework: for the first time,
practitioners have a clear 10-principle, 28-requirement structure to implement against.

---

## Qiita-Specific Patterns

### Tutorial-first, reflection-second: the Qiita content gradient

The most striking structural difference from Zenn: Qiita's content **gradient runs from tutorial
to reflection**, while Zenn's runs from opinion to analysis. The high-LGTM Qiita articles
(Article 8: 439 LGTMs, Article 3: 99 LGTMs) succeed either by being the most accessible
explanation of a concept, or by being the most practical operational guide. Deep critical
analysis on Qiita gets limited amplification unless it provides immediately actionable takeaways.

This creates a selection effect: practitioner frustrations that reach high LGTM counts on Qiita
tend to be those that come with a concrete solution framework (harness engineering, PlanGate,
Zero Trust architecture). Frustrations expressed without a proposed solution gain less traction.

### "Framework sprawl" as the primary architectural anxiety

Across multiple Qiita articles, a pattern emerges that is nearly absent from Zenn: anxiety about
**which framework to use and whether the choice creates lock-in or brittleness**.

Article 11 evaluates five major frameworks (LangGraph, CrewAI, AutoGen, OpenAI Agents SDK, Google
ADK) with specific guidance on which suits which task type. Article 6 synthesizes the major
"multi-agent controversy" positions from platform vendors. Article 9 tracks the migration from
OpenAI Swarm to Agents SDK. Article 5 specifically warns about "agent sprawl" — the proliferation
of agent implementations across teams without governance.

This is a Qiita-specific concern because Qiita readers are more likely to be making active
framework selection decisions in their daily engineering work. Zenn authors, often writing at
a more architectural level, abstract away from specific framework choices.

### "Harness engineering" as Qiita's original conceptual contribution

Article 3 (@miruky) introduces the term "harness engineering" as a distinct concept beyond
"context engineering" — and this framing appears to be Qiita-native rather than translated from
Western discourse. The concept addresses multi-session agent stability specifically, which is a
problem that emerges from sustained production use rather than from research or PoC environments.

The fact that this article achieved 99 LGTMs — the highest in this survey outside the widely-read
Oracle Japan survey article — suggests the concept resonated with practitioners who had hit this
wall in practice.

### Supply chain security as a Japan-visible risk

Article 13 (@kai_kou) presents an unusually specific risk surface: agent skill supply chains lack
the security infrastructure that npm/PyPI have built for traditional software packages. The
22,511-skill audit finding that "hook settings auditing does not exist across any current registry"
is a Japan-visible observation because the Japanese developer community is more concentrated (fewer
but larger engineering teams) and the risk of a single compromised skill spreading widely is
proportionally higher.

This concern is absent from both Zenn's organizational-governance framing and from HN/Reddit's
architecture-focused discourse.

### METI and the AI Promotion Act frame the compliance conversation

Qiita's governance articles are more implementation-oriented than Zenn's but cite the same
Japanese regulatory bodies. The METI AI Guidelines v1.1 and the AI Promotion Act (effective
June 2025) provide a Japan-specific compliance framework that Qiita authors treat as actionable:
Article 15 (@ymaeda_it) builds a full 28-requirement checklist with ISO 42001 cross-mapping.

This is distinctly Japanese: neither HN nor Reddit discuss METI guidelines, and even Zenn's
regulatory references are more at the level of "FSA/MHLW are watching" rather than
"here is the checklist."

### Cost framing: token economics made visible through production failures

Qiita practitioners think about cost through **production failure modes** more than
per-seat pricing comparisons (Zenn's frame). Article 5 provides the clearest signal: 60% of
production failures are rate-limit exceeded errors — meaning that at scale, cost governance
failures become operational failures. Article 14 cites a $50,000/day AWS bill from an infinite
loop agent as the headline risk.

This shifts the cost conversation from "how expensive is the subscription" to "what happens when
cost controls fail at runtime." It is a more engineering-centric framing of the same concern.

---

## Differences from Zenn / HN / Reddit

| Dimension | Reddit | HN | Zenn (Japanese) | Qiita (Japanese) |
|---|---|---|---|---|
| Primary skeptical frame | "The hype is what's broken" | Historical hype cycle comparisons | RPA failure déjà vu | **Engineering control gap**: harness failures, observability absences |
| Locus of concern | Technical: cost explosions, framework lock-in | Technical: cascading degradation, auditability | **Organizational**: readiness, governance, regulation | **Implementation-level**: multi-session stability, review velocity, supply chain |
| Content gradient | Use-case reports and tool comparisons | Architecture debates and first-principles analysis | Opinion/analysis → practitioner reflection | **Tutorial → operational guide** |
| Framework stance | Named but not primary | Skeptical of framework proliferation | Abstract (framework-agnostic) | **Actively evaluating specific frameworks** (LangGraph, CrewAI, AutoGen, ADK) |
| Human oversight framing | Accepted reluctantly | Philosophically analyzed | Engineered proactively as layered architecture | **Gated by plan approval** before generation starts |
| Cost vocabulary | Per-token, per-run | ROI arithmetic | Per-user-per-month, procurement | **Failure-mode-visible**: rate limits, infinite loops, sprawl |
| Regulatory specificity | Abstract | Abstract | Named agencies (FSA, MHLW) | **Checklist-level** (METI v1.1, ISO 42001, AI Promotion Act) |

---

## Alignment with Reyn's Design

### How Reyn's principles answer Qiita's voices

| Qiita concern | Reyn's design response |
|---|---|
| "Agents lose context mid-session and cannot recover" (Article 3) | P5: Workspace is the single source of truth; all inter-phase state persists in the workspace and survives session boundaries |
| "Technical anti-patterns spread exponentially across multi-session runs" (Article 3) | P6: every state change emits an event; the append-only event log provides the audit surface for detecting regression across sessions |
| "Agents prematurely declare completion without testing" (Article 3) | P2/P3: OS validates LLM output against the phase's declared schema; premature finish is rejected if final_output doesn't match `skill.final_output_schema` |
| "Debugging failures becomes impossible without tracing" (Articles 11, 18) | P6: event log captures every LLM payload and response; `REYN_LLM_TRACE_DUMP` provides the full observability surface |
| "60% of production failures are rate-limit exceeded errors" (Article 5) | BudgetTracker + FP-0003: per-agent budget limits; exhaustion triggers `ask_user` before rate limits are reached |
| "Function-calling selection errors at scale" (Article 8) | P4: LLM selects only from OS-provided candidates; arbitrary tool selection is structurally impossible |
| "AI cannot write a single line of code without plan approval" as a design goal (Article 16) | `ask_user` stdlib op: creates explicit human approval records at phase boundaries; plan approval is a first-class checkpoint |
| "Agent skill supply chain: hook auditing does not exist" (Article 13) | Skill resolution order (project → local → stdlib) provides an explicit trust hierarchy; skill permissions are gated by the OS |
| "$50k/day from an infinite loop agent" (Article 14) | BudgetTracker enforces spend limits per agent; the agent cannot exceed its declared budget without an explicit `ask_user` approval |
| "Agent sprawl: uncontrolled proliferation across teams" (Article 5) | P7: OS has no skill-specific strings; skills are declared, versioned artifacts loaded by name through the skill resolution order |

### "Harness engineering" as the name for what Reyn's OS provides

Article 3's harness engineering framework — environment initialization, incremental progress,
multi-layer feedback loops, codebase-as-context, technical debt control — maps to Reyn's
architecture at each level:

```
Harness engineering pillar (Article 3)     Reyn implementation
─────────────────────────────────────────  ──────────────────────────────────────────────
Environment initialization                 Phase preprocessor: deterministic setup before LLM call
Incremental progress (one feature/session) Skill graph: bounded transitions between declared phases
Four-layer feedback loops                  Phase validation: schema enforcement after each LLM output
Using codebase itself as context           P5 Workspace: all artifacts available to subsequent phases
Controlling technical debt amplification   P6 events: replay-capable log enables regression detection
```

Qiita's practitioner community arrived at the concept of "harness engineering" from the bottom up —
from hitting the wall in production. Reyn approaches the same structure from the top down —
from architectural principle. The convergence is the strongest signal in this survey.

### "PlanGate" as a Qiita-native pattern that Reyn implements structurally

Article 16's "PlanGate" — mandatory plan approval before AI generates code — is a practitioner
workaround for the review-velocity problem. Reyn implements this as a structural property:

```
PlanGate (Article 16's practitioner workaround):
  - Define Purpose, In scope, Out of scope, Review focus before generation
  - "AI cannot write a single line of code without plan approval"

Reyn OS (existing implementation):
  - ask_user creates an explicit approval record before any consequential phase transition
  - Phase input_schema declares what must be present before the LLM is called
  - Transitions are only allowed along the graph declared in the Skill
```

The difference: PlanGate is a team convention. Reyn makes it structurally enforced.

### Problems Qiita Identifies That Reyn Hasn't Solved Yet

**1. Multi-session context stitching is not yet a solved primitive**

Article 3's harness engineering addresses multi-session stability through environmental design.
Reyn's P5 workspace provides the persistence layer, but there is no stdlib skill for context
summarization, state snapshotting, or session-boundary recovery (PR21 crash recovery is planned
but not shipped). The "context loss causing regressions" failure mode identified by Qiita
practitioners remains partially unaddressed.

**2. Framework-agnostic skill composition for tool-heavy agents**

Qiita practitioners actively choose between LangGraph, CrewAI, AutoGen, and OpenAI Agents SDK.
Reyn's architecture is framework-agnostic by design (P7), but there is no guidance or example
skill showing how an agent that requires specialized tool orchestration (e.g., complex SQL
generation, multi-step web scraping) should be structured. The framework-selection anxiety
Qiita articles express has no Reyn-side answer yet.

**3. Supply chain trust for external skills**

Article 13's supply chain audit finding — no registry provides hook auditing — applies to Reyn's
own skill ecosystem. The skill resolution order provides a trust hierarchy (project > local >
stdlib), but there is no cryptographic verification, hook inspection, or automated security
scanning for skills loaded from external sources. This is a gap if Reyn builds a public skill
registry.

**4. Review-velocity tooling is not a Reyn primitive**

Article 16's PlanGate pattern is structurally supported by Reyn's `ask_user`, but the review
experience — what the human reviewer sees, how scope is communicated, how "Out of scope"
is enforced — is not yet designed. The bottleneck Qiita practitioners identify ("the diff humans
must review grows larger as AI writes faster") requires a review-UX primitive that does not exist
in Reyn's current design.

---

## Summary

Qiita's 2025-2026 AI agent discourse is more implementation-proximate than Zenn's and more
operationally specific than HN/Reddit's. Its distinctive contribution is the **implementation
control layer**: the recognition that the unsolved problems are not organizational readiness
(Zenn) or technical architecture at a conceptual level (HN), but the concrete failure modes
that appear when agents run unsupervised across sessions — context loss, premature completion,
technical debt amplification, supply chain exposure, and review bottlenecks.

The community has independently converged on "harness engineering" (Article 3) and "PlanGate"
(Article 16) as practitioner-invented patterns for the same gap. That both of these independently
arrived at ideas that Reyn's OS implements structurally is the clearest validation in this survey.

The framing that is most likely to resonate on Qiita:

> "Reyn is harness engineering built into the runtime — not a convention your team has to
>  enforce, but a structural guarantee your skill declares and the OS enforces."

For the OSS launch, Article 3's "harness engineering" vocabulary and Article 16's "PlanGate"
pattern are the bridging concepts: practitioners already understand the problems Reyn solves,
but have been implementing the solutions manually. Reyn makes them structural.
