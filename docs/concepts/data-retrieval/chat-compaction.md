---
type: concept
topic: [chat, compaction, context-window]
audience: [human, agent]
---

# Chat compaction

How Reyn keeps long chat sessions from overflowing the context window.

## What it is

When context fills, the middle of the history is folded into a rolling
structured summary. Three zones are fed to the LLM:

- **Head** — earliest UNCOVERED turns (raw; preserves the original task context)
- **Body** — rolling summary produced by the compaction engine
- **Tail** — most-recent turns (raw, kept for recency)

Head and tail sizes are **token-budgeted** — derived from `component_weights`
against the model's actual context window, not a fixed turn count. Chat fills
the window raw first; compaction fires only once an ACTUAL overflow is
measured (never predicted from a local estimate — #5528, see "Compaction
paths" below), but its effect is **permanent** once it does (#4954): every
turn at or below the resulting `covers_through_seq` watermark is excluded
from every subsequent LLM-facing projection — never resent raw, regardless
of whether a LATER turn's token total drops back under the trigger.
`history.jsonl` itself is untouched by this — a covered turn is excluded
from the projection only, still fully readable via
`extend_history_backward`; only what the LLM SEES permanently shrinks.

The `CompactionEngine` is an OS-internal Python helper that makes a direct
LLM call to produce the summary. It is not a stdlib workflow.

## Compaction paths

Compaction can be triggered by two independent paths. Both use the same
`CompactionEngine` and Head/Body/Tail slice logic.

#5528 (owner ruling): a THIRD path used to exist — a synchronous pre-frame
guard that estimated the current history's token usage before each router
LLM call and proactively force-compacted when the estimate exceeded the
effective trigger, before the LLM frame was even built. Removed, same
family as #5367's elide removal: a local token estimate cannot know what
the actual provider payload will look like (system prompt, tool schemas,
transport wrapping, inline media), so acting on the estimate risked
compacting a conversation that would have fit fine — #5296 decided this
in principle and #5528 carried it out. What that guard used to ALSO do —
refusing a single new message outright when IT ALONE is too large (drops
nothing; the message is never accepted then summarized away) — is
`ContextBudgetAdvisor.enforce_new_msg_budget`, kept because it is a
genuinely different behavior (owner's own "force close" — #4381 PR-4),
not compaction.

### 1. Voluntary compact op (LLM-requested)

When the window is filling, the OS injects a `## Context window` header with
the exact-token free window (the context-size signal). The model may emit a
`compact` Control IR op in response, which triggers an on-demand compaction
and returns the freed tokens and new headroom.
See [`control-ir.md`](../../reference/runtime/control-ir.md) for the op contract.

### 2. `retry_loop` overflow recovery

When the router's actual LLM call raises a context-length error, `retry_loop`
takes over — the ONLY path that recovers from an overflow now that the
pre-frame guard is gone (#5528). The decreasing measure is
`head`/`tail` token count, not the raw middle in isolation — the raw middle can
grow (content moves in from `head`/`tail` when those are still above their
minimum) and is compacted separately, split into smaller slices on repeated
failure down to a one-turn floor. `head`/`tail` never grow, so this measure
terminates: once both are at or below their minimum budgets and the raw middle
cannot be split any smaller, `retry_loop` raises a structured `UnrecoveredError`
rather than continuing over-budget or silently dropping content. This is a
structured-failure guarantee, not a success guarantee: `retry_loop` always
stops with a well-defined error instead of looping forever or silently losing
content — it does not promise the request ultimately fits.

## Overflow recovery

When a request overflows, reyn does not fail the turn: it shrinks and retries.
This is a **structured-failure** guarantee, not a success guarantee — recovery
either fits the request or stops with a named impossibility, and never loops
forever or silently drops content.

### Two failure sites, one way in

Recovery is entered from exactly one place: `retry_loop` is called only after
the *router's own* call raised something `classify_llm_failure` classifies as
`OVERFLOW` (#5577 — both this entry gate and the retry-arm inside
`_router_main_call` classify through the same function, not
`is_context_overflow_error`'s own keyword fallback alone, so a FATAL or
RETRYABLE cause whose message text merely resembles an overflow no longer
enters here). A `compact()` overflow is therefore never a way in — it is a
failure **nested inside** a recovery already under way.

The two sites still shrink **different payloads**, so they draw from different
candidates. Conflating them touches a compartment that was not even sent.

| | payload that was rejected | what may be shrunk |
|---|---|---|
| `main_call` overflow (the way in) | `SP` · `head` · `summary` · `tail` · `new_msg` | `head`, `tail` |
| `compact()` overflow (nested inside it) | the leading slice of `raw_middle` | `raw_middle` |

`raw_middle` is never on the wire — `main_call` does not receive it. A turn left
in mid is therefore neither sent nor rescued by relocation: it is compacted, or
the call fails. This is why the mid floor has no "defer to tail" escape.

`SP`, `new_msg` and the running `summary` are **reserved**: this call may not
shrink them. The reservation is structural, not a filter — `new_msg` is never
passed to the candidate builder at all, so no later edit can accidentally offer
it. Reserved does **not** mean "excluded from folding": a summary inside the
span being folded is folded like any other entry and replaced by a newer one,
and the reservation is then recomputed.

### Which failures enter the ladder at all

Shrinking is a remedy for **size**. Spending it on a failure that has nothing to
do with size burns LLM calls on a dead cause — and because a successful
`compact()` folds turns into a summary irreversibly, it degrades the history to
treat a problem the history did not cause. Failures are classified before the
ladder, not inside it:

| class | examples | what happens |
|---|---|---|
| **Overflow** | context-window error, HTTP 413, a response cut off by an output cap (`JSONDecodeError`) | enters the ladder |
| **Retryable** | 5xx, timeout, rate limit, connection error, HTTP 200 with a body that fails to parse as JSON (#5568 — a transport/protocol failure, unconditionally, regardless of what keywords the broken body happens to contain) | retried with backoff; never shrinks |
| **Fatal** | `TypeError` / `AttributeError` / `KeyError`, authentication, model-not-found | propagates unchanged; never shrinks |

Both arms classify through the same `classify_llm_failure` (#5543/#5531 §10) —
the `compact()` arm used to wrap *every* exception except quota exhaustion as
an overflow regardless of what it actually was; it now raises `Retryable`/
`Fatal` causes bare, same as `main_call`. One asymmetry remains, disclosed
rather than fixed: `compact()`'s own LLM call is not yet wired into the
router's backoff-retry wrapper, so a `Retryable` cause reaching it is
correctly re-raised bare (never shrunk) but has not been retried even once —
a separate gap from misclassification, not a consequence of it.

### Byte limits and token limits take the same path

An HTTP 413 is a request-**body-byte** limit; a context-window error is a
**token** limit. They are observed differently (`status_code == 413` is a real
attribute, never a substring match) and **reported** differently — an operator
facing a byte limit has a different remedy, so `UnrecoveredError.saw_byte_limit`
and the human-readable message keep the distinction, and a caller may branch on
that field after the terminal.

Inside the ladder they are **the same cause**. Every rung applies to both: the
same spill, the same slice sizing, the same room halving, the same terminals. A
rung guarded on the cause shape is a rung whose guard states *why it is needed*
for one cause — never why it is *forbidden* for the other.

### The ladder

Position order for spill candidates is `head` → `mid` → `tail`, largest-first
within each stage; the open turn is never a candidate. Mechanism order is
`compact > spill`: a compacted span stays visible as prose, while a spilled turn
leaves only a path the model must read back through a tool.

```text
overflow (byte or token — same path)
 1. spill      the WHOLE highest-priority Spillability tier → retry the SAME call
               (FIRST_CHOICE first; LAST_RESORT only once FIRST_CHOICE is
               exhausted) — fall through only when no un-spilled candidate
               is left in either tier
 2. slice      halve the count of mid turns offered to compact()
               floor = one turn
 3. refill     tail → mid, or head → mid, whichever is still above its minimum
 4. room       halve it and re-derive the floors, so 3 can fire again
               room = candidate - SP - new_msg - summary
 5. terminal   (a) or (b)
```

Rung 1 spills a **whole tier** per retry, not one candidate (#5592 — a real
machine with 2469 spillable candidates cost ~2469 `compact()` calls under the
old one-at-a-time shape; the whole-tier batch costs at most 2, one per tier).
`chat.compaction.spill_granularity: turn` reverts to the pre-#5592
one-candidate-per-call shape as an escape hatch — **not the safer choice**: the
upstream call count then scales with candidate count, and a rejected request
is still billed. Its population is the slice `compact()` is actually about to
send this attempt (`raw_middle[:_attempt_len]`) — coincides with the whole of
`raw_middle` on the first attempt, and is the offered slice only once rung 2
has halved at least once (#5592, correcting an earlier claim that the
population was always `raw_middle` entirely) — a spill is persistent, so
spilling a turn already shrinks a later fold's input regardless of which
attempt offered it.

The consequence is that a spill may leave *this* attempt unchanged. Progress is
therefore defined as **"at least one candidate was consumed"**, never as "the
wire got smaller": reading bytes here would mistake "this lever alone did not
visibly help" for "no lever is left".

### Slice sizing is a two-way search

The number of leading mid turns offered to `compact()` **halves on failure and
doubles on success**, capped at what mid still holds.

Both directions are needed, for opposite input shapes. A uniformly
hard-to-compact input must not re-discover its working slice size from full
after every success — so a success does not reset to full. A single dominant
turn is the other case: halving down to one succeeded *because* that turn
dominated, and once it is folded the remainder may well compact in one call — so
a success must be able to grow the slice again, or the rest of mid is folded one
turn per LLM call.

The value is **episode-scoped**. Returning to `main_call` means a `compact()`
succeeded, which means the whole is strictly smaller than when the episode
began — the size that failed then describes a history that no longer exists. The
next overflow starts the search again from the full remainder, and pays for it.

### Terminals

Recovery stops on a **predicate** — "no lever is left" — never on a budget.

| | meaning |
|---|---|
| **(a)** | mid is one turn, already spilled, and still will not fit alongside the summary |
| **(b)** | room is exhausted: `SP` and the newest message alone no longer fit, and neither is ever shrunk |

**Which terminal was reached travels as a structured value, not as a distinct
exception type** — this repo merged four diagnoses into one type once and
renamed a correct diagnosis into a wrong one; a field survives that merge, a
type does not.

Termination without an iteration cap rests on two nested measures:

- **within an episode** — un-spilled candidates strictly decrease (rung 1),
  `len(head) + len(tail)` strictly decreases (rung 3), room halving is bounded
  below by its own floor (rung 4).
- **across episodes** — total turn count strictly decreases: reaching
  `main_call` requires a successful `compact()`, and that absorbs at least one
  turn into the summary permanently.

Rung 1 is the one rung that does not move content between compartments, so it
cannot borrow their monotonicity. Two obligations follow:

- content that is already a spill preview must never be offered to spill again —
  re-offloading a preview produces a *different* preview, forever;
- "already tried" must record only candidates a spill actually consumed, never
  ones skipped as ineligible — an ineligible candidate becomes eligible the
  moment the eligibility predicate changes.

### Invariants

1. A summary **represents one contiguous span** and sits **where that span was**.
2. A summary is **not a fourth region.** History is one ordered list; some
   entries have `role == "summary"`. `head`/`mid`/`tail` are **windows over that
   list** — change the room and the windows move, so a summary can fall in
   `head` or in `tail`.
3. A span **grows only from its neighbours** (head's end, tail's start).
4. The **open turn is never handed to the shrinker** — by construction.
5. `SP`, `new_msg` and `summary` are **reserved** from the windows' share; only
   the remainder is apportioned.

### See also

- **#5531** — the canonical expected behaviour and the remaining diff.
- **#5514** — which content may be dropped, and in what order.
- **#3783** — the failure classification this section's first table describes.

## What the compaction produces

The `CompactionEngine` folds new turns into five sections with per-section
token budgets (derived from `section_weights`):

| Section | What it captures |
|---------|-----------------|
| `topic_arc` | High-level thread of the session |
| `decisions` | Agreed-on choices and constraints |
| `pending` | Open tasks and unresolved questions |
| `session_user_facts` | Stable facts about the user or project |
| `artifacts_referenced` | Files read, URLs fetched, MCP tool calls (path/line level) |

`covers_through_seq` is derived deterministically by the compaction postprocessor
and the result is appended as a `role: "summary"` entry in `history.jsonl`.

`"summary"` is reyn's own internal vocabulary — the discriminator watermark/
trim/spill logic reads — never a value a provider recognises as a chat role.
The normal turn path never leaks it to the wire (it attaches the summary via
a separate, already-`"assistant"`-role synthetic turn instead); the overflow-
recovery path (`retry_loop`, both the compact()-origin fold and a pre-
existing persisted summary reached through `decompose_history_for_retry`)
maps it to `"assistant"` at its own wire-egress point (`_router_main_call`)
before it ever reaches `loop.run` (#5598 — a provider that validates role
names rejects an un-mapped `"summary"` outright, with a 400 in ~2 seconds,
before inference even starts, regardless of payload size: the turn right
after a compaction succeeds always failed).

Token budgets use `litellm.token_counter` by default for accuracy; a cheaper
`len(text) // 4` heuristic is available for latency-sensitive deployments
(`use_chars4_estimate: true`). A third path is automatic, not operator-set:
if `litellm.token_counter` fails (e.g. a genuinely unreachable network),
`estimate_tokens()` falls back to the same `len(text) // 4` heuristic for a
60-second cooldown window, then automatically re-probes the real tokenizer —
this needs no configuration and is not a permanent switch (#4395).

## Compaction axis

The engine serves the chat axis (conversation history, this document): both
automatic compaction (per-frame) and an on-demand seam (the `compact` Control
IR op, available to the LLM when the context-size signal fires).

## Cost observability

The `/budget` command shows token and cost usage broken down **by purpose**
(`compaction`, `judge`, `dogfood`) plus agent-attributed buckets. This lets
operators see how much of their token spend the compaction engine is consuming
across a session.

## Configuration (`reyn.yaml`)

```yaml
chat:
  compaction:
    # Budget allocation: integer weights, normalised at runtime.
    # Keys: head / body / tail / new_msg / compaction_batch
    component_weights:
      head:             10
      body:             5
      tail:             15
      new_msg:          10
      compaction_batch: 60

    # Section budget weights within body, normalised at runtime.
    section_weights:
      topic_arc:            5
      decisions:            40
      pending:              25
      session_user_facts:   10
      artifacts_referenced: 35

    # Hard cap on summary body tokens (post-truncation).
    body_token_cap: 1500

    # Set true to use len(text)//4 instead of litellm.token_counter.
    use_chars4_estimate: false
```

Weights are sum-arbitrary — any positive integers work; Reyn normalises them at
startup. Larger values give more token budget to that component.

**Self-consistency guard:** budgets (head/tail/new_msg/body — and, derived
from them, `B_M` and the compaction trigger `effective_trigger`) are computed
against the *selected model's* context window, not a fixed number. If
`component_weights` reserve too much of the window (in particular a large
`body`/summary weight, or a large system prompt eating into the same pool)
for a small-context model, `effective_trigger` or `B_M` can be computed as
non-positive. `CompactionEngine` fails fast at construction time in that case
(`CompactionBudgetSelfConsistencyError`, raised — not asserted, so the guard
survives `python -O`) rather than silently letting compaction fire on every
turn. The error message reports the model's context window, the resolved
`component_weights`, and the computed budgets, so the fix is to reduce
`component_weights` (or use a model with a larger context window) — never to
clamp the computed value.

**Removed keys:** `head_size`, `tail_size`, `trigger_total_tokens`, and
`min_compact_batch` are no longer recognised. If present in `reyn.yaml`, Reyn
emits a `DeprecationWarning` and ignores them. Remove these keys from your
config — head/tail sizing is now token-budget via `component_weights`, and
auto-compaction is window-relative.

## Trade-offs

**Preserved:** topic arc, decisions, pending items, user facts, referenced
artifacts (including tool activity — files read / URLs fetched / MCP tools
called surface as `artifacts_referenced` entries when the result is
conversation-relevant), and the raw head and tail zones (token-budgeted,
sized relative to the model's actual context window).

**Lost:** verbatim phrasing of compacted turns; exact ordering of minor
exchanges. Section budgets are soft — slight overruns self-correct on the
next compaction pass.

### Tool-aware compaction

`new_turns` includes `role="assistant"` entries with `tool_calls` and
`role="tool"` response entries. The compaction engine sees these as structured
input and decides whether to record the call under `artifacts_referenced`. Tool
turns count toward the head/tail/body slice the same as plain conversational
turns.

Compaction runs synchronously before the frame (path 1) or on-demand (path 2).
Events `compaction_started` / `compaction_completed` / `compaction_failed` are
emitted to the session event log (P6).

## See also

- `src/reyn/services/compaction/engine.py` — `CompactionEngine` implementation
- `src/reyn/runtime/services/compaction_controller.py` — chat-axis wiring
- [Control IR: compact](../../reference/runtime/control-ir.md#compact) — LLM-requested compact op
- [Events](../../reference/runtime/events.md)
