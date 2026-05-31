---
type: concept
topic: [chat, compaction, context-window]
audience: [human, agent]
---

# Chat compaction

How Reyn keeps long chat sessions from overflowing the context window.

## What it is

When enough turns accumulate, the middle of the history is folded into a
rolling structured summary. Three zones are fed to the LLM:

- **Head** — first N turns (raw, never compacted; preserves the original task context)
- **Body** — rolling summary produced by the compaction engine
- **Tail** — last N turns (raw, kept for recency)

The `CompactionEngine` is an OS-internal Python helper that makes a direct
LLM call to produce the summary. It is not a stdlib skill.

## Compaction paths

Compaction can be triggered by four independent paths. All four use the same
`CompactionEngine` and Head/Body/Tail slice logic.

### 1. Synchronous pre-frame guard

Before each router LLM call, `_maybe_force_compact_for_router` checks the
estimated token usage of the current history against the effective trigger
budget. If over budget, it calls `force_compact_now` synchronously before the
LLM frame is built. This ensures the prompt never exceeds the budget *before*
the call — a proactive shrink rather than a reactive one.

### 2. Background post-reply path

After each reply, `CompactionController.spawn_maybe` fires a background task
when both conditions hold: estimated middle-turn tokens exceed
`trigger_total_tokens` (default 30 000) and at least `min_compact_batch` turns
(default 5) are available to absorb. This path is fire-and-forget and never
blocks the current turn.

### 3. Voluntary compact op (LLM-requested)

When the window is filling, the OS injects a `## Context window` header with
the exact-token free window (the context-size signal). The model may emit a
`compact` Control IR op in response, which triggers an on-demand compaction on
the current axis (chat or phase) and returns the freed tokens and new headroom.
See [`control-ir.md`](../../reference/runtime/control-ir.md) for the op contract.

### 4. `retry_loop` overflow backstop

When the pre-frame guard's token estimate under-counts and the router raises a
context-length error, `retry_loop` takes over. It shrinks head, tail, and the
raw middle in bounded iterations (max 8) until the prompt fits or all budgets
are exhausted. If exhausted, it raises a structured `UnrecoveredError` — never
a silent over-budget prompt. This is the dead-end-free guarantee: the
conversation cannot overflow into an unrecoverable state.

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
(`use_chars4_estimate: true`).

## Compaction axes

The same engine serves three distinct compaction axes:

- **Chat axis** — conversation history (this document).
- **Planner step axis** — older plan-step results inside an active plan.
- **Phase axis** — older `control_ir_results` inside a running phase's act loop.

Each axis has both automatic compaction (per-frame or per-reply) and an
on-demand seam (the `compact` Control IR op, available to the LLM when the
context-size signal fires).

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

    # Background trigger (spawn_maybe path only).
    trigger_total_tokens: 30000
    min_compact_batch: 5

    # Hard cap on summary body tokens (post-truncation).
    body_token_cap: 1500

    # Set true to use len(text)//4 instead of litellm.token_counter.
    use_chars4_estimate: false
```

Weights are sum-arbitrary — any positive integers work; Reyn normalises them at
startup. Larger values give more token budget to that component.

## Trade-offs

**Preserved:** topic arc, decisions, pending items, user facts, referenced
artifacts (including tool activity — files read / URLs fetched / MCP tools
called surface as `artifacts_referenced` entries when the result is
conversation-relevant), and the raw first/last N turns.

**Lost:** verbatim phrasing of compacted turns; exact ordering of minor
exchanges. Section budgets are soft — slight overruns self-correct on the
next compaction pass.

### Tool-aware compaction

`new_turns` includes `role="assistant"` entries with `tool_calls` and
`role="tool"` response entries. The compaction engine sees these as structured
input and decides whether to record the call under `artifacts_referenced`. Tool
turns count toward the head/tail/body slice the same as plain conversational
turns.

Compaction runs in a background asyncio task (path 2) or synchronously before
the frame (path 1). Events `compaction_started` / `compaction_completed` /
`compaction_failed` are emitted to the session event log (P6).

## See also

- `src/reyn/services/compaction/engine.py` — `CompactionEngine` implementation
- `src/reyn/chat/services/compaction_controller.py` — chat-axis wiring
- [Control IR: compact](../../reference/runtime/control-ir.md#compact) — LLM-requested compact op
- [Events](../../reference/runtime/events.md)
