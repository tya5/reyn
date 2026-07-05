---
type: concept
topic: runtime
audience: [human, agent]
search_hints: [pipeline, pipeline DSL, control plane, execution plane, driver-as-session, PipelineExecutorDriver, crash recovery pipeline, run_pipeline, safety by structure, Turing-incomplete]
---

# Pipelines

A **pipeline** is a deterministic, multi-step control flow written in a small
YAML DSL: a fixed sequence of `transform` / `tool` / `agent` steps, optionally
composed with a handful of structural primitives (`call`, `match`, `fold`,
`for_each`). An agent launches a pipeline the same way it calls any other
tool — by name, with an input — and the pipeline runs to completion (or
failure) under its own crash-recoverable execution, independent of whatever
else the launching agent does next.

Pipelines exist because not every multi-step task should be re-derived by an
LLM turn by turn. A recurring, well-understood procedure — fan out a review
across N reviewers and merge their verdicts, walk a list applying the same
transform, retry-then-escalate a flaky check — is more reliable, cheaper, and
auditable as a *written* control flow than as an agent re-planning it from
scratch every time. A pipeline is that written control flow: the steps and
their composition are fixed in the DSL; only the *data* flowing through them
varies at run time.

## Control plane vs execution plane

Reyn already has a non-deterministic execution plane: an agent turn, where an
LLM decides what to do next from the tools and context available to it. A
pipeline is a **separate, deterministic control plane** layered alongside it:

- The **execution plane** (an agent turn) is where judgment lives — an LLM
  reads context and picks an action. Its control flow is not fixed in
  advance; it emerges from the model's decisions.
- The **control plane** (a pipeline) is where a known-shape procedure lives —
  its steps and their composition (sequence, branch, fan-out, accumulate) are
  fixed in the DSL. An `agent` step is the seam between the two: a pipeline
  step can delegate one bounded piece of judgment to an LLM (a capability-
  narrowed leaf worker), but the pipeline itself never improvises its own
  shape.

This separation is what makes a pipeline **Turing-incomplete by design**: the
primitives compose (a `call` can invoke another pipeline, a `fold` can run a
`call` as its per-item step) but there is no general recursion, no dynamic
step generation, and no primitive that lets a running pipeline rewrite its
own step list. A pipeline's full step graph is knowable by reading its DSL
document — it cannot construct new control flow at run time the way an agent
turn can decide to call an unanticipated tool.

## Safety by structure

Because the control flow is fixed and closed, several safety properties fall
out of the DSL's shape rather than needing a runtime policy layered on top:

- **No nested launch.** A pipeline `tool` step cannot itself launch another
  pipeline or delegate to another agent — nesting is `call`-only. This keeps
  the cost-bound approval an agent grants when it launches a pipeline a
  transitive closure over a *known* step graph, not an open-ended one a
  running step could extend.
- **Capability narrowing is structural, not a runtime check.** An `agent`
  step's ephemeral session is spawned under the *invoker's own identity* and
  narrowed restrict-only — a pipeline step can never exceed the capability
  envelope of the agent that launched it, by construction. For an ad-hoc
  pipeline an agent generates on the fly (see
  [Invocation](pipeline-registration.md)), a step naming a different agent's
  identity would be a capability escalation, so a static gate rejects that
  before anything spawns.
- **Fan-out is bounded, not unbounded-by-omission.** `for_each`'s concurrent
  branches are capped by an operator-set spawn budget (see
  [Pipeline DSL reference § Safety caps](../../reference/runtime/pipeline-dsl.md)),
  charged for every `agent` step reached anywhere in the run — top-level or
  fanned out — since those steps spawn ephemeral sessions outside the normal
  spawn-lineage bookkeeping.

## Driver-as-session

A pipeline does not run inline on the launching agent's own turn. Launching
one — via any of `run_pipeline`, `run_pipeline_async`, `run_pipeline_inline`,
or `run_pipeline_inline_async` — spawns a dedicated session running a
`PipelineExecutorDriver`, and the pipeline executes inside *that* session.

This is a deliberate reuse of the ordinary session substrate rather than a
bespoke execution path: the driver-session's run-loop, inbox, WAL journaling,
and crash-restore machinery are the exact same ones a chat session uses — the
driver just interprets a "turn" as a run/resume nudge instead of a user
utterance to route through an LLM. The practical payoff is that pipeline
crash-recovery rides infrastructure that already has to be correct for every
other session, rather than a second recovery path that could drift out of
sync with it.

Two ways an agent can relate to a launched pipeline's run:

- **Sync / attached** (`run_pipeline`, `run_pipeline_inline`): the caller
  attaches to the driver-session's run and waits for it to reach a terminal
  state in-band — live step-progress events stream to the caller for the
  duration (what a TUI live view renders), and a cooperative Ctrl-C stops the
  run cleanly at the next step boundary rather than killing it mid-step. If
  the process crashes while attached, the run itself is not lost — recovery
  resumes it, and the result is delivered later as an inbox message instead.
- **Async / detached** (`run_pipeline_async`, `run_pipeline_inline_async`):
  the caller gets `{status: started, run_id}` immediately and the result
  arrives later as an inbox message, whenever the run reaches a terminal
  state.

## Crash recovery

Crash-recovery is the pipeline feature's differentiator relative to just
having an agent re-plan the same procedure every time: a pipeline run is
resumable exactly where it left off, without re-running steps that already
had a side effect.

Two pieces make this work:

- **A per-run work order**, persisted (as `invocation.json`) *before* the
  first step runs. It carries everything needed to reconstruct the run from
  nothing — the pipeline definition itself (so a resume needs no external
  registry, even for an ad-hoc inline pipeline), the seed input, the reply
  address, and any schemas its `verify: schema` steps validate against.
- **Step-boundary generation snapshots**, recorded after every step
  completes: the run's pipe data, named stores, and the set of steps already
  completed. A resume reads the latest snapshot and replays every step
  already recorded as complete — including partial progress inside a `call`,
  `match`, `fold`, or `for_each` (each records its own sub-steps under a
  nested key), so a crash mid-composition does not re-fire an already-landed
  side effect. Recovery is thus **exactly-once execution**: a step's side
  effect fires once, no matter how many times the run is resumed. Delivery of
  the *final result* to the reply address, by contrast, is **at-least-once**
  — a crash between the last step finishing and the result being posted
  re-delivers on the next recovery pass, so the caller never silently loses a
  result, at the cost of a caller needing to tolerate a duplicate delivery.

See [Pipeline registration](pipeline-registration.md) for how a pipeline
definition reaches a session in the first place, and the
[Pipeline DSL reference](../../reference/runtime/pipeline-dsl.md) for the
normative step/primitive grammar and the four invocation tools.

## What's out of scope today

Hot-reloading a running session's pipeline registry when `pipelines/` changes
on disk is not yet built. Rewind/fork semantics for an in-flight or completed
pipeline run are a deferred, separate concern from the crash-recovery
exactly-once guarantee described above.

## See also

- [Pipeline registration](pipeline-registration.md) — how a pipeline
  definition is loaded from disk and made launchable.
- [Pipeline DSL reference](../../reference/runtime/pipeline-dsl.md) — the
  normative step/primitive grammar, the expression language, and the
  invocation tools.
- [Capability profiles](capability-profile.md) — the capability floor a
  pipeline launch is gated by.
