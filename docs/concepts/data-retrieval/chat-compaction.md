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
rather than continuing over-budget or silently dropping content. A safety cap
also bounds the iteration count independently of this. This is a
structured-failure guarantee, not a success guarantee: `retry_loop` always
stops with a well-defined error instead of looping forever or silently losing
content — it does not promise the request ultimately fits.

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
