# Write and run a pipeline

A pipeline is a small YAML file describing a deterministic, multi-step
control flow. This guide walks through writing one, dropping it into your
project, and invoking it — plus the ad-hoc, no-registration alternative for a
one-off procedure an agent generates on the fly. For the full grammar and
invocation-tool reference, see the [Pipeline DSL reference](../../reference/runtime/pipeline-dsl.md);
for the why/architecture, see [Pipelines](../../concepts/runtime/pipelines.md).

## 1. Write the pipeline

Create a `pipelines/` directory at your project root (the default scan
directory) and drop in a `*.yaml` file. This one takes a `name`, greets it,
and shouts the result:

```yaml
# pipelines/greet.yaml
pipeline: greet
description: Greet a name and shout it.
steps:
  - transform: {value: "'Hello, ' + ctx.name + '!'", output: greeting}
  - tool: {name: shell, args: {command: !expr "'echo ' + greeting"}, output: shouted}
```

A few things worth noting about this file:

- The pipeline registers under the name in its `pipeline:` key (`greet`), not
  the file name — `pipelines/greet.yaml` could be renamed to anything and
  still register as `greet`.
- Each step is a single-key mapping naming its kind (`transform`, `tool`,
  `agent`, or one of the [compositional primitives](../../reference/runtime/pipeline-dsl.md#compositional-primitives)).
- `ctx.name` is the seed input this pipeline expects; `greeting`, once
  written by the first step's `output`, becomes available as `ctx.greeting`
  in later steps — here referenced bare as `greeting` inside the `!expr`
  string-concat, since the second step's context still exposes it as a named
  store.
- `!expr` marks `command` as an expression to evaluate, not a literal string
  — see [Literals vs `!expr`](../../reference/runtime/pipeline-dsl.md#literals-vs-expr).

## 2. Start (or restart) the session

Pipelines are registered from disk at session start — there's no separate
"install" step and no `reyn.yaml` entry required for the default `pipelines/`
directory. Restart your session (or start a fresh one) and `greet` is
registered.

If a file fails to parse, or two files declare the same `pipeline:` name,
session start fails loudly, naming the offending file — a typo never
silently drops a pipeline you meant to ship. See
[Pipeline registration § Failure behavior](../../concepts/runtime/pipeline-registration.md#failure-behavior-fail-loud)
for the full table.

## 3. Invoke it

An agent can launch `greet` either through the plain tool call:

```
run_pipeline(name="greet", input={name: "Reyn"})
```

or the qualified catalog verb the action catalog surfaces for every
registered pipeline:

```
pipeline__greet({name: "Reyn"})
```

Both block until the pipeline finishes and return its final output — here,
the shouted greeting. Live step-progress is visible in the TUI for the
duration of the run, and Ctrl-C stops it cleanly at the next step boundary
rather than killing it mid-step.

### Sync vs async

If the procedure is long-running and you don't want to block on it, use the
async form instead:

```
run_pipeline_async(name="greet", input={name: "Reyn"})
```

This returns `{status: "started", run_id: "..."}` immediately; the result
arrives later as a `[pipeline]` message in your conversation. Use `run_pipeline`
when you want the result inline and are fine waiting; use `run_pipeline_async`
for a fire-and-forget launch. Both are equally crash-recoverable — a process
restart mid-run resumes exactly where it left off rather than re-running
completed steps (see [Pipelines § Crash recovery](../../concepts/runtime/pipelines.md#crash-recovery)).

## 4. Ad-hoc, no-registration alternative

Sometimes a procedure is one-off — worth writing as a pipeline for its
crash-recovery and structural safety properties, but not worth registering as
a file. `run_pipeline_inline` (and its async counterpart
`run_pipeline_inline_async`) take the same DSL as a `pipeline:` document, but
as a string an agent generates at call time:

```
run_pipeline_inline(
  definition="""
    pipeline: adhoc_greet
    steps:
      - transform: {value: "'Hi, ' + ctx.name", output: greeting}
  """,
  input={name: "Reyn"},
)
```

The definition is parsed and run through a static-analysis gate — schema
references resolve, tool names resolve, no step launches another pipeline or
delegates, and any `agent` step runs only under the invoker's own identity —
**before** anything is spawned. A bad definition fails clearly and spawns
nothing; a good one is exactly as crash-recoverable as a registered pipeline,
since its full definition travels with the run's own recovery state. See
[Ad-hoc inline launch](../../reference/runtime/pipeline-dsl.md#ad-hoc-inline-launch)
for the complete gate checklist.

## A worked end-to-end example: fan out then merge

A slightly larger example putting `for_each` and `match` to use — review a
document with several reviewers in parallel, then branch on whether they all
agreed:

```yaml
# pipelines/review.yaml
pipeline: review
description: Fan a document out to reviewers, then branch on the verdict.
steps:
  - for_each:
      over: ctx.reviewers
      max_parallel: 4
      on_error: "retry(1)"
      do:
        agent:
          prompt: "Review this document as {item}: {ctx.doc}. Reply with passed (bool) and notes (string)."
          schema: Review
      collect: {transform: {value: "pipe"}}
      output: reviews
  - transform: {value: "all(reviews, r -> r.passed)", output: all_passed}
  - match:
      on: all_passed
      cases:
        "True": {pipeline: report_pass, pass: [reviews]}
        "False": {pipeline: report_fail, pass: [reviews]}
      output: report
---
schema: Review
fields:
  passed: {type: bool}
  notes: {type: string}
```

Launch it with a list of reviewer identities and a document:

```
run_pipeline(name="review", input={reviewers: ["reviewer_a", "reviewer_b"], doc: "..."})
```

Each reviewer runs as an isolated, concurrent `agent` step (up to 4 at once,
each retried once on failure); once all have landed, `all_passed` folds them
into one boolean via the R1 `all()` combinator, and `match` routes to a
`report_pass` or `report_fail` sub-pipeline accordingly (both would need to
be registered separately, or replaced with plain `transform`/`tool` steps for
a self-contained single-file version).
