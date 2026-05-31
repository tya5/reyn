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

- **Head** — earliest turns (raw, never compacted; preserves the original task context)
- **Body** — rolling summary produced by the compaction engine
- **Tail** — most-recent turns (raw, kept for recency)

Head and tail sizes are **token-budgeted** — derived from `component_weights`
against the model's actual context window, not a fixed turn count. Chat fills
the window raw first; compaction fires only once the history exceeds the
effective trigger, which is window-relative (derived from the same budgets, not
an absolute token count).

The `CompactionEngine` is an OS-internal Python helper that makes a direct
LLM call to produce the summary. It is not a stdlib skill.

## Compaction paths

Compaction can be triggered by three independent paths. All three use the same
`CompactionEngine` and Head/Body/Tail slice logic.

### 1. Synchronous pre-frame guard

Before each router LLM call, `_maybe_force_compact_for_router` checks the
estimated token usage of the current history against the effective trigger
budget (window-relative). If over budget, it calls `force_compact_now`
synchronously before the LLM frame is built. This ensures the prompt never
exceeds the budget *before* the call — a proactive shrink rather than a
reactive one.

### 2. Voluntary compact op (LLM-requested)

When the window is filling, the OS injects a `## Context window` header with
the exact-token free window (the context-size signal). The model may emit a
`compact` Control IR op in response, which triggers an on-demand compaction on
the current axis (chat or phase) and returns the freed tokens and new headroom.
See [`control-ir.md`](../../reference/runtime/control-ir.md) for the op contract.

### 3. `retry_loop` overflow backstop

When the pre-frame guard's token estimate under-counts and the router raises a
context-length error, `retry_loop` takes over. It shrinks head, tail, and the
raw middle monotonically toward their minimum budgets — terminating by
construction, because each iteration reduces some shrinkable budget — until the
prompt fits. When all budgets reach their floor, it raises a structured
`UnrecoveredError` rather than continuing over-budget. A safety cap bounds the
iteration count but is rarely the limiting factor. This is the dead-end-free
guarantee: the conversation cannot overflow into an unrecoverable state.

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

Each axis has both automatic compaction (per-frame) and an on-demand seam (the
`compact` Control IR op, available to the LLM when the context-size signal fires).

## Cost observability

The `/budget` command shows token and cost usage broken down **by purpose**:
`main`, `phase`, `compaction`, `judge`, and agent-attributed buckets. This lets
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
- `src/reyn/chat/services/compaction_controller.py` — chat-axis wiring
- [Control IR: compact](../../reference/runtime/control-ir.md#compact) — LLM-requested compact op
- [Events](../../reference/runtime/events.md)
