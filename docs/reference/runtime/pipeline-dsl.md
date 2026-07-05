---
type: reference
topic: runtime
audience: [human, agent]
search_hints: [pipeline DSL, pipeline grammar, transform step, tool step, agent step, call step, match step, fold step, for_each step, expr, R1 expression, verify schema, run_pipeline, run_pipeline_async, run_pipeline_inline, safety.spawn.max_pipeline_fan_out_depth, safety.spawn.max_pipeline_spawns]
---

# Pipeline DSL reference

Normative grammar for a pipeline definition — the step kinds, the
compositional primitives, the expression language they evaluate against, the
schema/`verify: schema` mechanism, and the four tools that launch a pipeline.
See [Pipelines](../../concepts/runtime/pipelines.md) for the why/architecture,
and [Pipeline registration](../../concepts/runtime/pipeline-registration.md)
for how a definition reaches a session.

## Document shape

A pipeline definition is one or more `---`-separated YAML documents:

- Exactly one `pipeline:` document — the pipeline itself.
- Zero or more `schema:` documents — named schemas the pipeline's steps can
  reference via `verify: schema` (see [Schemas](#schemas-verify-schema)).

```yaml
schema: Review
fields:
  passed: {type: bool}
  notes: {type: string}
---
pipeline: review_and_report
description: Review a document and summarize the verdict.
steps:
  - agent: {prompt: "Review {ctx.doc}. Reply with passed/notes.", schema: Review, output: review}
  - transform: {value: "review.passed and 'OK' or 'NEEDS WORK'", output: verdict}
```

### `pipeline:` document keys

| Key | Required | Meaning |
|-----|----------|---------|
| `pipeline` | yes | The declared name. Authoritative for registration and for a `call`/`match` step's target — see [Pipeline registration](../../concepts/runtime/pipeline-registration.md#the-declared-name-is-authoritative). |
| `description` | no | Human-readable summary; surfaced to the LLM alongside the name when a registered pipeline is listed as a `pipeline__<name>` catalog action. Defaults to empty. |
| `steps` | yes | Non-empty list of steps, executed in order (see [Step kinds](#step-kinds) and [Primitives](#compositional-primitives)). |

`input`, `defaults`, and `refine` are part of the pipeline design's fuller
grammar but have no runtime yet — a document using them fails to parse with
an explicit "not yet supported" error rather than being silently ignored.

## Step kinds

Every step is a single-key mapping naming its kind. Three are **linear leaf
steps** — they read the context, do one piece of work, and produce a result:

### `transform`

A pure step: `value` is evaluated as an [R1 expression](#the-r1-expression-language)
against the current context; the result becomes this step's pipe data (and,
if `output` is set, is also written to that named store).

```yaml
- transform: {value: "'Hello, ' + ctx.name + '!'", output: greeting}
```

| Key | Required | Meaning |
|-----|----------|---------|
| `value` | yes | An R1 expression source. |
| `output` | no | Named store to write the result to. |

### `tool` (+ `shell` sugar)

A side-effecting step: dispatches `name` with `args` through the same
qualified-action-routing-then-bare-lookup a live `invoke_action` call uses —
so a `tool` step can name either a qualified action (`file__read`) or a bare
registered tool name (`web_search`).

```yaml
- tool: {name: web_search, args: {query: !expr ctx.brief, limit: 5}, output: results}
```

| Key | Required | Meaning |
|-----|----------|---------|
| `name` | yes | The tool/action name (literal string). |
| `args` | no | Mapping of argument name → value. Each value is a **literal** unless tagged `!expr` (see [Literals vs `!expr`](#literals-vs-expr) below). |
| `schema` | no | A registered schema name the result must conform to (`verify: schema` — see [Schemas](#schemas-verify-schema)). Non-conformance fails the step. |
| `output` | no | Named store to write the result to. |

`shell` is sugar for a `tool` step named `"shell"`:

```yaml
- shell: {command: !expr "'ls ' + ctx.dir", output: listing}
```

| Key | Required | Meaning |
|-----|----------|---------|
| `command` | yes | Literal or `!expr`, same rule as a `tool` step's `args` values. |
| `schema` | no | Same as `tool`. |
| `output` | no | Same as `tool`. |

#### Literals vs `!expr`

A `tool`/`shell` argument value is a **literal** — passed through to the tool
exactly as written — unless it is tagged with the YAML tag `!expr`:

```yaml
args: {query: !expr ctx.brief, limit: !expr "ctx.n + 1", label: "a plain string"}
```

`query` and `limit` are R1 expression sources, resolved against the step's
context at run time; `label` is the literal string `"a plain string"`.
`!expr` is only honored as the **whole value** of an argument — one hiding
inside a nested list or mapping is a parse error, so there is no ambiguity
between "a literal that happens to look like an expression" and "an
expression."

`transform.value` is always an R1 expression (no `!expr` tag needed — there is
no literal form for a `transform` step). An `agent` step's `prompt` is never
an R1 expression — see below.

### `agent`

An LLM-driven leaf step: `prompt` (a template string) is interpolated against
the current context and run as one turn in an ephemeral session,
capability-narrowed to `capabilities` (or the invoker's own profile if
omitted) under `identity` (or the invoker's own identity if omitted).

```yaml
- agent: {prompt: "Summarize: {ctx.doc}", capabilities: {tools: [file__read]}, schema: Summary, output: summary}
```

| Key | Required | Meaning |
|-----|----------|---------|
| `prompt` | yes | A template string — `{ctx.dotted.path}` / `{pipe}` references are interpolated (values only, no operators — this is string interpolation, not an R1 expression). |
| `identity` | no | The agent identity to run under. Defaults to the run's invoker. A **registered** pipeline may name any identity; an **inline, agent-generated** pipeline may only name the invoker's own identity — naming another agent's identity is rejected by the static-analysis gate as a capability escalation (see [Ad-hoc inline launch](#ad-hoc-inline-launch)). |
| `capabilities` | no | `{tools: [NAME*]}` — narrows the ephemeral session's tool surface. Restrict-only: a pipeline step can never exceed the invoker's own envelope. |
| `schema` | no | Same `verify: schema` semantics as `tool`, applied to the parsed JSON reply. |
| `output` | no | Named store to write the result to. |

Every `agent` step, wherever it is reached (top-level or fanned out inside a
`for_each`), charges the run's shared spawn budget — see
[Safety caps](#safety-caps).

## Compositional primitives

Five primitives compose steps into non-linear control flow — the full
Appendix-B set, all supported today.

### `call` — sub-pipeline

Synchronously runs a **registered** sub-pipeline by static name and threads
its final output out as this step's result.

```yaml
- call: {pipeline: validate_doc, pass: [doc, rules], output: validation}
```

| Key | Required | Meaning |
|-----|----------|---------|
| `pipeline` | yes | A static literal pipeline name — never a runtime expression. An unregistered target fails the step. |
| `pass` | no | List of this pipeline's named-store names to expose to the callee. The callee's context is built **fresh** from only these names — a store not listed here is structurally invisible to the callee. A name absent from the caller's stores fails the step. |
| `output` | no | Named store to write the callee's final result to. |

The callee's first step receives the caller's pipe data at the call site; the
callee's own final step output becomes this `call` step's result. A callee
failure fails the `call` step.

### `match` — runtime-selected sub-pipeline

Evaluates `on` to a value, selects the case whose label string-equals it, and
runs that case's target exactly like a `call` step.

```yaml
- match:
    on: "review.passed"
    cases:
      "True": {pipeline: report_pass, pass: [review]}
      "False": {pipeline: report_fail, pass: [review]}
    default: {pipeline: report_unknown}
    output: report
```

| Key | Required | Meaning |
|-----|----------|---------|
| `on` | yes | An R1 expression evaluated against the current context; its stringified result selects a case label. |
| `cases` | yes | Non-empty mapping of `LABEL: {pipeline, pass?}` — each target a static literal name, exactly like `call`. |
| `default` | no | `{pipeline, pass?}` run when no case label matches. A step with no matching case and no `default` fails. |
| `output` | no | Named store to write the selected callee's result to. |

Every case/`default` target is a static literal — the runtime value only ever
selects a *label*, never a target directly.

### `fold` — sequential accumulator

Walks a list in order, threading an accumulator through a repeated `do` step.

```yaml
- fold:
    over: ctx.items
    init: "0"
    do: {transform: {value: "acc + item"}}
    output: total
    max_items: 1000
```

| Key | Required | Meaning |
|-----|----------|---------|
| `init` | yes | An R1 expression evaluated once, before the first iteration, seeding `acc`. |
| `do` | yes | A single step re-invoked once per list item, in a context of `{ctx, pipe, item, acc}` — `item` is the current element, `acc` the running accumulator; `do`'s return value becomes the next `acc`. |
| `output` | yes | Named store for the final `acc` (a `fold`'s whole point is producing a named result — required, unlike `call`'s optional `output`). |
| `over` | no* | An R1 expression resolving to the list to walk. |
| `items` | no* | A static literal list. |
| `max_items` | no | Caps the walk to the first N elements (a longer source is silently truncated, never an error). |

\* `over` and `items` are mutually exclusive; if neither is given, the list
falls back to the step's incoming pipe data. Item failure fails the whole
fold. There is no `collect` (unlike `for_each`) — each item's result depends
on the accumulated state of the ones before it, so there is nothing to
collect independently.

### `for_each` — concurrent fan-out

Runs `do` over each list item as an isolated concurrent sub-scope, then runs
`collect` once over the ordered results.

```yaml
- for_each:
    over: ctx.reviewers
    max_parallel: 4
    on_error: "retry(2)"
    do: {agent: {prompt: "Review as {item}: {ctx.doc}", schema: Review}}
    collect: {transform: {value: "pipe"}}
    output: reviews
```

| Key | Required | Meaning |
|-----|----------|---------|
| `do` | yes | A step run once per item, in a context of `{ctx, pipe, item}` — `ctx` is an isolated **copy** of the outer named stores (no sibling visibility between items), `pipe` is this step's own incoming pipe data held constant across every item. |
| `collect` | yes | A step run once, after the fan-out, over the ordered list of surviving item results (its `pipe` context). Its result is this step's overall result. |
| `on_error` | yes | One of `continue` (a failed item is dropped from the results, never re-run on resume), `abort` (a failed item cancels the still-pending items and fails the whole step), or `retry(N)` (re-run the failed item up to N more times, then fall back to `abort`). |
| `over` | no* | Same as `fold`. |
| `items` | no* | Same as `fold`. |
| `max_parallel` | no | Caps live concurrency (a `Semaphore`). Omitted, defaults to a conservative finite value — never unbounded by omission. |
| `output` | no | Named store to write `collect`'s result to. |

\* `over`/`items` are mutually exclusive, falling back to incoming pipe data
like `fold`. There is no `item`-level `acc` (that is `fold`-only) — an item
cannot see any other item's result.

### `parallel` — heterogeneous named-branch fan-out

`for_each`'s heterogeneous sibling: instead of fanning one `do` step out over
a runtime-sized list, `parallel` fans a static, finite set of *distinct*
named branches out concurrently, then runs `collect` once over the named map
of their results.

```yaml
- parallel:
    on_error: "abort"
    branches:
      security: {agent: {prompt: "Security-review {ctx.doc}", schema: Review}}
      style: {agent: {prompt: "Style-review {ctx.doc}", schema: Review}}
    collect: {transform: {value: "{'security': security, 'style': style}"}}
    output: reviews
```

| Key | Required | Meaning |
|-----|----------|---------|
| `branches` | yes | A non-empty `{NAME: Step}` mapping — each branch is its own, independently-shaped step (a different kind/config per name), unlike `for_each`'s one `do` re-invoked per item. Every branch runs concurrently; the branch count itself is the concurrency bound (no `max_parallel` — the set is statically finite). |
| `collect` | yes | A step run once, after every branch lands, over the **named map** `{branch_name: result}` (not an ordered list, unlike `for_each`). Its result is this step's overall result. |
| `on_error` | no | One of `continue`, `abort` (the default when omitted — unlike `for_each`, where `on_error` is required), or `retry(N)` — same semantics as `for_each`'s `on_error`. A `continue`-dropped branch's key is absent from `collect`'s named map. |
| `output` | no | Named store to write `collect`'s result to. |

Each branch's context is `{ctx, pipe}` — `ctx` an isolated copy of the outer
named stores, `pipe` this step's own incoming pipe data held constant across
every branch. There is no `item`/`acc` (those are `for_each`/`fold`-only) and
no sibling visibility between branches.

## The R1 expression language

`transform.value`, a `tool`/`shell` argument tagged `!expr`, and `match.on`
all resolve against the same small, **total** expression language (R1) — a
purpose-built tree-walking interpreter, not a general scripting language and
not a code-execution sandbox. It has no recursion, no user-defined functions,
no unbounded loops (every combinator iterates one already-materialized list
exactly once), no IO, and no `eval`/`exec`.

**Literals**: `true` / `false` / `null`, integers, floats, single- or
double-quoted strings.

**Field refs**: a dotted path against the context, e.g. `ctx.review.passed`
or bare `pipe`. A missing path or a non-mapping intermediate segment raises —
bare paths are not safe navigation; use `get(...)` for that (below).

**Operators**: `and` / `or` / `not`; comparisons `==` `!=` `<` `>` `<=` `>=`
(`<`/`>`/`<=`/`>=` require two numbers or two strings; `==`/`!=` work on
anything); arithmetic `+` `-` `*` `/` (numeric; `+` also concatenates strings
and lists). Division by zero raises.

**Combinators** — the only call-like syntax the grammar has, a fixed closed
set:

| Combinator | Signature | Meaning |
|---|---|---|
| `map` | `map(list, item -> expr)` | Transform each element. |
| `filter` | `filter(list, item -> expr)` | Keep elements where the lambda is true. |
| `all` | `all(list, item -> expr)` | True iff every element satisfies the lambda. |
| `any` | `any(list, item -> expr)` | True iff some element satisfies the lambda. |
| `find` | `find(list, item -> expr)` | First matching element, or `null`. |
| `count` | `count(list)` | Element count. |
| `sum` | `sum(list)` | Numeric sum. |
| `join` | `join(list, sep)` | String-join. |
| `get` | `get(base, "dotted.path", default?)` | **Safe** navigation — unlike a bare `Path`, never raises on a missing path; returns `default` (or `null`) instead. |

A `lambda` (`item -> expr`) is only ever valid as the direct argument of
`map`/`filter`/`all`/`any`/`find` — it is not a value that can be assigned or
passed around, and naming anything outside this fixed combinator set as a
function call is a parse error.

Example expressions: `"'Hello, ' + ctx.name + '!'"`, `"ctx.n + 1"`,
`"all(ctx.reviews, r -> r.passed)"`.

An `agent` step's `prompt` is a **different** mechanism: a template string
where `{ctx.dotted.path}` / `{pipe}` references are interpolated as plain
values — not R1 expressions, no operators inside the braces.

## Schemas — `verify: schema`

A schema names a nested, monomorphic type: a set of fields, each a scalar
(`bool`/`string`/`number`), an `enum`, a typed `list` (its element type,
`of`, is mandatory — no untyped lists, and lists-of-lists are not allowed), a
nested inline `object`, or a `ref` to another registered schema (a
recursive-reference cycle across the registered set is rejected at
registration time).

```yaml
schema: Review
fields:
  passed: {type: bool}
  notes: {type: string}
  tags: {type: list, of: {type: string}}
```

A `tool`/`shell`/`agent` step's `schema: NAME` key names a registered schema
its result (or, for `agent`, its parsed JSON reply) must conform to —
non-conformance fails the step. Schemas declared in the same DSL document set
(standalone `schema:` documents) are what makes this possible for an ad-hoc
[inline pipeline](#ad-hoc-inline-launch) too, since its schemas travel with
the same definition string.

## Invocation

Four tools launch a pipeline. All four converge on the same execution: a
launch spawns a dedicated `PipelineExecutorDriver` session and the pipeline
runs inside it (see [Driver-as-session](../../concepts/runtime/pipelines.md#driver-as-session)) —
none of them run a pipeline inline on the caller's own turn.

| Tool | Registered / inline | Sync / async |
|------|---------------------|---------------|
| `run_pipeline` | Registered, by `name` | Sync — attached, blocks until terminal |
| `run_pipeline_async` | Registered, by `name` | Async — detached, returns immediately |
| `run_pipeline_inline` | Inline, ad-hoc `definition` string | Sync — attached, blocks until terminal |
| `run_pipeline_inline_async` | Inline, ad-hoc `definition` string | Async — detached, returns immediately |

### Registered launch

`run_pipeline(name, input?)` and `run_pipeline_async(name, input?)` look a
pipeline up by its registered name (see
[Pipeline registration](../../concepts/runtime/pipeline-registration.md)).
`input` seeds the pipeline's initial named context (`ctx.*`) for its first
step; omit it for a pipeline that needs no seed input. A `name` that isn't
registered fails clearly.

### Sync vs async

- **Sync** (`run_pipeline`, `run_pipeline_inline`): the caller attaches to the
  driver-session's run and blocks until it reaches a terminal state, reading
  the result back in-band (`{status: "ok", data: {run_id, output,
  named_stores}}`, or `error`/`cancelled`). Live `pipeline_step_started` /
  `pipeline_step_completed` events stream to the caller for the run's
  duration (what a TUI live view renders), and a cooperative Ctrl-C stops the
  run cleanly at the next step boundary. If the attach itself is interrupted
  by a crash, the run is not lost — it is handed to the same recovery path
  async uses, and the result arrives later as an inbox message instead
  (`{status: "started", data: {run_id}}`).
- **Async** (`run_pipeline_async`, `run_pipeline_inline_async`): returns
  `{status: "started", data: {run_id}}` immediately; the final result arrives
  later as a `[pipeline]` inbox message.

### Ad-hoc inline launch

`run_pipeline_inline(definition, input?)` and
`run_pipeline_inline_async(definition, input?)` take a pipeline DSL string
the calling agent generates at run time — the same Appendix-B grammar as a
registered pipeline file, including any `schema:` documents the definition's
own steps reference. There is no pre-registration: the string is parsed and
run through a **static-analysis gate** before anything is spawned, so a bad
definition fails clearly and spawns nothing:

1. The definition parses.
2. Every step `schema:` reference resolves within the definition's own
   schemas.
3. Every `tool` step's name resolves to a registered tool or qualified
   action.
4. *(Structural, not runtime-checked)* the driver-session spawns under the
   invoker's own identity and narrows restrict-only, so a generated pipeline
   can never exceed the invoker's own envelope by construction.
5. No `tool` step launches a pipeline or delegates — nesting is `call`-only.
6. **Inline-only**: an `agent` step's `identity`, if set, must equal the
   invoker's own identity. A registered pipeline is exempt from this check (a
   trusted registrant deliberately chose the identity); an inline,
   agent-generated one naming a different identity is a capability
   escalation and is rejected.

An inline run is crash-recoverable identically to a registered one — its
full parsed definition (including its schemas) is persisted into the
work-order, so recovery never needs to re-parse or look anything up.

## Safety caps

Two operator-set caps in `reyn.yaml`'s `safety.spawn` block bound a pipeline
run's fan-out, threaded into every `run`/`resume` call:

```yaml
# reyn.yaml
safety:
  spawn:
    max_pipeline_fan_out_depth: 5   # default
    max_pipeline_spawns: 100        # default
```

| Key | Default | Meaning |
|-----|---------|---------|
| `max_pipeline_fan_out_depth` | `5` | Maximum **nesting depth** of `for_each` fan-out scopes (a top-level `for_each` is depth 1; a `for_each` inside another's `do`/`collect` is depth 2; …). A `for_each` that would exceed this fails the step rather than spawning. `0` = unlimited. |
| `max_pipeline_spawns` | `100` | Maximum number of ephemeral sessions **one pipeline run** may spawn across all its `agent` steps — top-level or fanned out via `for_each`. A per-run monotonic counter; a spawn past the cap fails the step. `0` = unlimited. |

Both default to conservative finite values — a run is never unbounded by
omission. Neither cap is reachable by an LLM at run time; both are
operator-set and restart-only.

## Security

See [Pipeline registration § Security](../../concepts/runtime/pipeline-registration.md#security-launching-a-pipeline-stays-gated):
launching a pipeline (any of the four tools above) sits on the same
`HIGH`-severity, spawn-adjacent capability floor as delegating to another
agent. A context narrowed by the untrusted-content floor or an unbound
delegate's floor cannot launch a pipeline, registered or inline.
