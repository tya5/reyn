# Write and run a pipeline

A pipeline is a small YAML file describing a deterministic, multi-step
control flow. This guide walks through writing one, registering it, and
invoking it — plus the ad-hoc, no-registration alternative for a one-off
procedure an agent generates on the fly. For the full grammar and
invocation-tool reference, see the [Pipeline DSL reference](../../reference/runtime/pipeline-dsl.md);
for the why/architecture, see [Pipelines](../../concepts/runtime/pipelines.md).

## 1. Write the pipeline

Write an Appendix-B DSL file anywhere in your project (there is no default
scan directory — see step 2). This one takes a `name`, greets it, and shouts
the result:

```yaml
# pipelines/greet.yaml
pipeline: greet
description: Greet a name and shout it.
steps:
  - transform: {value: "'Hello, ' + ctx.name + '!'", output: greeting}
  - tool: {name: sandboxed_exec, args: {argv: !expr "['echo', ctx.greeting]"}, output: shouted}
```

A few things worth noting about this file:

- The pipeline registers under the name in its `pipeline:` key (`greet`), not
  the file name — `pipelines/greet.yaml` could be renamed to anything and
  still register as `greet`.
- Each step is a single-key mapping naming its kind (`transform`, `tool`,
  `agent`, or one of the [compositional primitives](../../reference/runtime/pipeline-dsl.md#compositional-primitives)).
- `ctx.name` is the seed input this pipeline expects; `greeting`, once
  written by the first step's `output`, becomes available as `ctx.greeting`
  in every later step. There is no bare-name shortcut — reading it as bare
  `greeting` (instead of `ctx.greeting`) fails the step, since every
  expression evaluates against a context whose only two top-level keys are
  `ctx` (all named stores) and `pipe` (the immediately-preceding step's own
  result). See [Data flow between steps](../../reference/runtime/pipeline-dsl.md#data-flow-between-steps)
  for the full rule and a worked trace.
- `!expr` marks the `argv` value as an expression to evaluate, not a literal
  list — see [Literals vs `!expr`](../../reference/runtime/pipeline-dsl.md#literals-vs-expr).
- `sandboxed_exec` runs `argv` in the operator's sandbox (argv-only — no
  shell interpretation); the previous step's pipe data can be threaded to its
  STDIN via an `stdin_pipe: !expr pipe` arg — this pipeline doesn't use that
  input, but see the
  [reference doc's `sandboxed_exec` step docs](../../reference/runtime/pipeline-dsl.md#tool-step-results)
  for the full STDIN/STDOUT contract.
- A `tool` step's result (here, `ctx.shouted`) is always the flat
  `{text: ..., structured: ...}` shape (`structured` only present for
  non-text data) — see [`tool` step results](../../reference/runtime/pipeline-dsl.md#tool-step-results).

## 2. Register it

Pipelines are registered purely via an explicit `pipelines.entries`
declaration in config — there is no directory scan, so a `*.yaml` file
sitting on disk is invisible to every session until it is registered. Add an
entry to `reyn.yaml` (the entry key must match the DSL's own declared
`pipeline:` name exactly):

```yaml
# reyn.yaml
pipelines:
  entries:
    greet:
      path: pipelines/greet.yaml
      description: "Greet a name and shout it"
```

or, equivalently, ask an agent to call
`pipeline_management__install_local(path="pipelines/greet.yaml")`, which
parses the file, validates the name, and writes the same kind of entry to
`.reyn/config/pipelines.yaml` for you. Either way the change takes effect at
the next turn boundary via hot-reload — **no session restart needed** to pick
up a newly-registered pipeline.

If the file fails to parse, or two entries declare the same `pipeline:` name,
loading fails loudly, naming the offending entry — a typo never silently
drops a pipeline you meant to ship. See
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
  - transform: {value: "all(ctx.reviews, r -> r.passed)", output: all_passed}
  - match:
      on: ctx.all_passed
      cases:
        "True": {pipeline: report_pass, pass: {reviews: ctx.reviews}}
        "False": {pipeline: report_fail, pass: {reviews: ctx.reviews}}
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
into one boolean via the R1 `all()` combinator — read from `ctx.reviews`,
the durable named store the `for_each` step's `output` wrote, not a bare
`reviews` — and `match` routes to a `report_pass` or `report_fail`
sub-pipeline accordingly (both would need to be registered separately, or
replaced with plain `transform`/`tool` steps for a self-contained single-file
version).

This second pipeline would need its own `pipelines.entries` declaration (or
`pipeline_management__install_local` call), same as step 2 above, before an
agent can launch it.

## 5. Manage and run pipelines from the CLI directly

Everything above happens through an agent's tool calls inside a chat session.
`reyn pipe` does the same three things — list, install, run — as a direct CLI
command, for when you want to manage or execute a pipeline without a live
session:

```console
$ reyn pipe list
No pipelines configured.
Add one with: reyn pipe install --path <file.yaml>  or edit reyn.yaml manually.

$ reyn pipe install --path pipelines/greet.yaml --non-interactive
Installing pipeline from path: pipelines/greet.yaml

Pipeline 'greet' installed successfully.
Config written to: .reyn/config/pipelines.yaml
...

$ reyn pipe list
NAME         PATH                      DESCRIPTION                  ENABLED  LOAD STATUS
──────────────────────────────────────────────────────────────────────────────────────
greet.greet  pipelines/greet.yaml      Greet a name and shout it    yes      loaded

$ reyn pipe run greet.greet --input '{"name": "Reyn"}'
{
  "pipe_data": {"text": "Hello, Reyn! (shouted)"},
  "named_stores": {
    "name": "Reyn",
    "greeting": "Hello, Reyn!",
    "shouted": {"text": "Hello, Reyn! (shouted)"}
  }
}
```

`reyn pipe install` also accepts `--source <git/GitHub URL>` (same `//subdir`
convention as `reyn mcp install`), and `--name` to assert the installed
pipeline's identity up front — a mismatch against the DSL's own declared
`pipeline:` name is refused with a clear error rather than silently diverging
the two.

`reyn pipe list`'s **NAME** column always shows the exact runnable name(s) —
what `reyn pipe run` accepts — for a `loaded` entry, so anything printed there
can be pasted straight into `reyn pipe run` without translation. `reyn pipe
run` also accepts the bare entry-key (`greet` instead of `greet.greet`) when
it unambiguously matches exactly one registered pipeline, printing a `note:
resolved '<key>' -> '<full-name>'` to stderr so the resolution is transparent;
if the key registers more than one pipeline, `run` reports the ambiguity and
lists the candidates rather than guessing.

`reyn pipe list`'s **LOAD STATUS** column is the direct way to see a broken
entry without digging through logs: an entry that is `enabled: true` but
failed to parse (bad DSL, missing file, a duplicate declared name) shows
`FAILED` right there (keyed by its entry-key, since nothing runnable was
registered), instead of just silently not appearing anywhere.

`reyn pipe run` executes the pipeline **standalone, in the CLI process
itself** — every step kind runs, including `tool:` and `agent:`. A `tool:`
step dispatches through a real, standalone tool-execution context (the same
routing a live session's tool step uses); an `agent:` step spawns a real,
short-lived ("ephemeral") session under the `default` agent and runs it to
completion, the same as a chat session's `agent:` step would. There is no
live chat REPL and no `--docker`/`--sandbox-backend` container option behind
`reyn pipe run` (host filesystem/exec only) — a pipeline that needs those
should run from `reyn chat`/`reyn run` instead. There is also no
crash-recovery for a `reyn pipe run` invocation: it is a one-shot foreground
command, so a killed/interrupted run is simply a failed command, the same as
any other CLI tool — not a resumable driver-session.
