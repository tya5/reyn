---
type: concept
topic: architecture
audience: [human, agent]
---

# Architecture overview

Reyn is an agent OS. Its defining bet: structural guarantees at the OS
layer — constrained transitions (P4), workspace-only data flow (P5),
append-only events (P6), OS-agnostic Skills (P7) — produce more
trustworthy agents than connectivity breadth or flexible orchestration
do. MCP, A2A, and Skills are capabilities Reyn provides; the OS
contract is what makes them auditable and reproducible.

```
User → Agent → Skill → OS → Phase → Workspace
                  ↘ Event (record everything)
```

## Layers

### Agent

Interprets user intent. Selects or generates a Skill. Does NOT execute phases.

In practice the "Agent" today is the CLI plus chat router — both are thin and route the user's input to a Skill.

### Skill

A directory of markdown + YAML files. Defines the phase graph and the final output schema. Does not contain executable code (except optional Python preprocessor steps, which are sandboxed).

Skill is **one capability group** in the runtime stack. The OS runs equally well with no skills, stdlib skills, or custom skills. The full capability inventory (Skills, RAG, code execution, MCP, A2A, safety, memory, permissions, …) is in [`docs/feature-map.md`](../../feature-map.md).

### Phase

A reusable processing unit. Declares only its `input` and instructions.

### OS

The runtime executor. Sole owner of control flow. See [../architecture/principles.md](../architecture/principles.md) P3 and P7.

### Workspace

The single source of truth for data. All files, tool outputs, and artifacts live here. Phases read/write via Control IR.

### Artifact

Structured data passed between phases. Validated against schemas declared in `artifacts/*.yaml`.

### Event

Every state change emits an event. Replayable for debugging and (eventually) checkpointing.

## The runtime loop

For each phase visit:

1. Build the context frame (instructions + input + candidate outputs + control ops).
2. Run preprocessor steps if any (deterministic — `reference/dsl/preprocessor.md`, Phase 2).
3. Call the LLM.
4. Receive: `next_phase | finish`, an artifact, optional Control IR ops.
5. Validate the output against OS rules and against the chosen target's schema.
6. Execute Control IR ops (file ops, ask_user, sub-skills, etc.).
7. Update workspace.
8. Emit events.
9. Transition or terminate.

## Why this shape?

Three properties fall out of the layering:

- **Replayability.** Because every state change is an event and the OS is the only mutator, a saved event log replays the same workflow deterministically (modulo the LLM call itself).
- **Skill portability.** Because the OS knows nothing about specific skills (P7), adding a new skill never changes OS code. Skills are pure data + LLM-readable instructions.
- **Bounded LLM creativity.** Because the LLM picks from a fixed set of OS-provided transitions (P4), it can't invent control flow that breaks invariants.

## Phase execution flow

The layered diagram above shows *what* the components are. This section shows
*what happens* during one Phase invocation — useful for new contributors mapping
their mental model, for debugging a Phase that doesn't behave as expected, and
for understanding the cost of one Phase tick.

```
User        Agent          OS Runtime         LLM (LiteLLM)   Workspace       Events
 │            │                │                    │               │              │
 │──message──>│                │                    │               │              │
 │            │──invoke skill──>│                    │               │              │
 │            │          ┌─────┴──── for each Phase visit ──────────────────────┐  │
 │            │          │     │                    │               │              │
 │            │          │     │──read artifacts────────────────────>│              │
 │            │          │     │<───────────────────────── context frame ─────────│  │
 │            │          │     │──────────────────────────────────────────────────── emit phase_started ──>│
 │            │          │     │                    │               │              │
 │            │          │     │──call(messages,────>│               │              │
 │            │          │     │    candidates, ops) │               │              │
 │            │          │     │<── {control,        │               │              │
 │            │          │     │     artifact,        │               │              │
 │            │          │     │     control_ir}      │               │              │
 │            │          │     │                    │               │              │
 │            │          │     ├── validate artifact (vs next-phase / final_output_schema)
 │            │          │     │                    │               │              │
 │            │          │     │  ┌── if validation fails ──────────────────────┐  │
 │            │          │     │  │──────────────────────────────────────────────── emit validation_error ─>│
 │            │          │     │  │──re-prompt─────>│               │              │
 │            │          │     │  └── (within max_phase_retries budget) ─────────┘  │
 │            │          │     │                    │               │              │
 │            │          │     ├── for each Control IR op ──────────────────────┐  │
 │            │          │     │  ├── permission check                          │  │
 │            │          │     │  │──────────────────────────────────────────────── emit <op>_started ────>│
 │            │          │     │  │──dispatch + write result──────>│              │
 │            │          │     │  │──────────────────────────────────────────────── emit <op>_completed ──>│
 │            │          │     │  └────────────────────────────────────────────────────────────────────────│
 │            │          │     │──────────────────────────────────────────────────── emit phase_completed ─>│
 │            │          │     │                    │               │              │
 │            │          │     ├── control.type == transition ──────────────────┐  │
 │            │          │     │  └── pick next phase from Skill graph; repeat ─┘  │
 │            │          │     │                    │               │              │
 │            │          │     ├── control.type == finish ──────────────────────┐  │
 │            │          │     │  ├── validate against final_output_schema       │  │
 │            │          │     │  │──────────────────────────────────────────────── emit skill_completed ──>│
 │            │          │     │  └────────────────────────────────────────────────────────────────────────│
 │            │          │     │                    │               │              │
 │            │          │     ├── control.type == abort ───────────────────────┐  │
 │            │          │     │  │──────────────────────────────────────────────── emit skill_aborted ───>│
 │            │          │     │  └────────────────────────────────────────────────────────────────────────│
 │            │          └─────┴────────────────────────────────────────────────┘  │
 │            │<───── final_output artifact ────────│               │              │
 │<─── reply ─│                │                    │               │              │
```

**Note on diagram rendering:** The diagram above uses ASCII art because
`pymdownx.superfences` is enabled in this docs build without `custom_fences`
configured for Mermaid. A Mermaid rendering of the same flow is available on
the project website architecture page.

### Step-by-step narration

1. **Context build (P5)** — The OS reads from the Workspace only what the Phase
   declares as input. Nothing leaks between phases through any other channel.

2. **LLM call** — The OS assembles the prompt (instructions + input artifact +
   `candidate_outputs` + `available_control_ops`) and calls the LLM. Single-shot
   by default; retried within `max_phase_retries` on validation failure.

3. **Output validation (P4)** — The artifact in the LLM's response must match
   the declared schema for the chosen destination: `next_phase.input_schema` on
   a transition, or `skill.final_output_schema` on a finish. The OS rejects any
   hallucinated phase name not in the Skill graph.

4. **Re-prompt loop** — If validation fails, the OS emits `validation_error` and
   re-prompts. The loop is bounded by `max_phase_retries`; exhausting retries
   fails the phase rather than crashing.

5. **Control IR execution (P3 + permissions)** — The OS dispatches each op in
   `control_ir` sequentially. Every op passes through the permission gate before
   dispatch. Denial emits `permission_denied` and returns a structured denial
   result; it does not abort the phase unless the LLM decides to abort.

6. **Workspace write (P5)** — Every op that produces data (file reads, web
   fetches, MCP calls, etc.) writes its result to the Workspace before the next
   op runs. In-memory results are not trusted between ops.

7. **Event emission (P6)** — Every state change is an event: `phase_started`,
   `phase_completed`, `validation_error`, `<op>_started`, `<op>_completed`,
   `skill_completed`, `skill_aborted`. The OS doesn't care about the LLM's
   reasoning; it cares that the transition was validated and recorded.

8. **Transition or finish** — On `transition`, the OS picks the next phase from
   the Skill graph and starts a new Phase visit. On `finish`, it validates the
   final artifact against `skill.final_output_schema`, emits `skill_completed`,
   and returns the artifact to the caller.

### Connection to act-sense-react

Each iteration of the outer Phase visit loop in the diagram above IS one full
act-sense-react cycle. **Act** is `control_ir` execution (the LLM's decision
dispatched by the OS). **Sense** is context-frame assembly from Workspace and
Events at the top of the next visit. **Re-act** is the next LLM call with the
updated context. The sequence diagram operationalizes what the act-sense-react
framing below summarises — the structural contract that makes the loop explicit
and OS-owned rather than implicit in the LLM's behaviour.

## Reyn through the act-sense-react lens

The broader agent community has converged on a working definition of what makes
a system an "agent": it must have the ability to affect the world, sense how it
affected the world, and choose to make additional actions — forming a closed
**act → sense → re-act feedback loop**. This framing was articulated prominently
in Tines's post ["What, exactly, is an 'AI Agent'? Here's a litmus
test"](https://www.tines.com/blog/a-litmus-test-for-ai-agents/) and the
accompanying HN discussion, where multiple commenters independently converged on
the loop formulation as the minimum requirement for agency.

Reyn implements this loop structurally, not nominally. Every step of the loop
maps to a concrete primitive:

| Loop step | Reyn primitive |
|-----------|----------------|
| **act** | Phase outputs `control_ir` — the LLM's decision, dispatched by the OS |
| **sense** | Workspace and Events, read by the next phase's context frame |
| **re-act** | LLM produces the next transition and artifact in the new context |
| **loop closure** | Skill graph `transitions` and finish condition |

The structural nature of this mapping is what distinguishes Reyn from frameworks
where the loop is implicit. In many agent systems, "sensing" is whatever the LLM
happens to read, "acting" is whatever tool it happens to call, and the loop
closes only because the LLM decides to keep going. Reyn makes each step
explicit and OS-owned:

- Workspace is the only sensing channel — what the LLM sees is exactly what the
  OS built into the context frame, no more.
- Events are the only audit record — every sense-act cycle leaves a replayable
  trace ([../runtime/events.md](../runtime/events.md)).
- Control IR is the only acting vocabulary — the LLM cannot invent new
  operations outside the declared op set.
- The Skill graph is the only re-act path — the LLM picks among OS-validated
  transitions; it cannot add a new edge mid-run ([../architecture/principles.md](../architecture/principles.md#p3-os-controls-execution)).

This is what [P3 (OS controls execution)](../architecture/principles.md#p3-os-controls-execution)
makes concrete in the loop framing: the OS owns the loop structure; the LLM
makes decisions inside it.

For readers mapping Reyn against the current agent landscape, two families
are relevant:

**General-purpose agents** (OpenClaw, Hermes, and similar connectivity-first
systems) make the opposite bet: optimize for reach. Rich integration catalogs,
flexible tool wiring, protocol-agnostic connectivity are their strengths. Reyn's
bet is orthogonal — optimize for structural integrity of the loop itself. Both
families implement MCP connectivity; what differs is what the runtime
guarantees about the loop. In connectivity-first systems, the loop contract
is absent by design — flexibility requires it. In Reyn, the contract is the
product.

**Workflow frameworks** (LangGraph, AutoGen, Semantic Kernel) expose the
act-sense-react loop as a programmable surface: graph edges, node functions,
agent-defined steps. Reyn encodes the same loop as an OS-validated contract:
transitions are validated against the Skill graph, artifacts against schemas,
and every state change against the event log before execution continues. The
LLM makes the same decisions it would in any of these systems; what differs
is whether the loop boundary is enforced by the OS or by the author's discipline.

## Kernel runtime layers (FP-0020)

`OSRuntime` is implemented as a thin wiring layer over four vertical
layers, each owning one depth-level of skill execution:

| Layer | Module | Responsibility |
|---|---|---|
| 1 (top) | `run_orchestrator.py` | Phase sequence + transitions + rollback + lifecycle |
| 2 | `phase_executor.py` | Act/decide loop for one phase + retry |
| 3 | `llm_call_recorder.py` | One LLM call + WAL recording + budget enforcement |
| state | `run_state.py` | Mutable run-scope state threaded through layers 1-3 |
| types | `runtime_types.py` | Exception types + helpers (leaf, no kernel deps) |

`OSRuntime.__init__` wires these layers (state → recorder → executor →
orchestrator) and `OSRuntime.run()` delegates to the orchestrator.

ChatSession is similarly decomposed into services under `chat/services/`:

- `compaction_controller.py`
- `skill_runner.py`
- `budget_gateway.py`, `chain_manager.py`, `intervention_registry.py`,
  `memory_service.py`, `router_host_adapter.py`, `snapshot_journal.py`
- `a2a_handler.py`, `intervention_handler.py`, `auto_resume_handler.py`

### Transport vs agent scoping

Two concepts that appear to affect what an agent "can do" operate at different layers and must not be conflated:

**Transport** (`a2a_handler.py`, MCP) is how messages arrive at the session — the wire protocol. A2A delivers a task to `ChatSession`; MCP delivers a tool invocation result. Neither grants nor restricts capabilities; they are routing mechanisms only.

**Agent scoping** is established at `ChatSession` construction time through its parameters: `env_backend` (sandbox profile), `exclude_tools` (tool suppression), and permission grants. These determine what the session is allowed to do with incoming messages, regardless of which transport delivered them.

The practical consequence: when a code path appears to "lack a capability," the gap is almost always in agent construction (scoping not configured, tool not wired, permission not granted) rather than in the transport layer. Extending transport rarely fixes a scoping gap; the fix belongs in the constructor call or the `reyn.yaml` configuration.

## See also

- [../architecture/principles.md](../architecture/principles.md) — the eight constraints
- [../architecture/phase-vs-skill-vs-os.md](../architecture/phase-vs-skill-vs-os.md) — responsibility boundaries
- [../runtime/workspace.md](../runtime/workspace.md) — Workspace in depth
- [../runtime/events.md](../runtime/events.md) — full event taxonomy
- [Reference: control-ir](../../reference/runtime/control-ir.md) — Control IR op semantics
- [Reference: llm-output-contract](../../reference/runtime/llm-output-contract.md) — the LLM JSON shape
- [Reference: events](../../reference/runtime/events.md) — event types
- [Agent engineering — seven lenses](../agent-engineering/index.md) — the same architecture through external engineering perspectives
