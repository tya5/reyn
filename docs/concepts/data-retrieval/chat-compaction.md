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

- **Head** — first `head_size` user/agent turns (raw, never compacted)
- **Body** — rolling summary produced by the `chat_compactor` skill
- **Tail** — last `tail_size` user/agent turns (raw, kept for recency)

## Trigger

`CompactionController.spawn_maybe` is called after every message. It fires
a background `_maybe_compact` task when both conditions hold:

1. Estimated tokens of uncovered middle turns exceeds `trigger_total_tokens` (default **30 000**)
2. The candidate set has at least `min_compact_batch` turns (default **5**)

Token estimation uses a cheap `len(text) // 4` heuristic.

## What the compactor produces

The `chat_compactor` stdlib skill folds new turns into five sections with
per-section token budgets: `topic_arc` (200), `decisions` (400),
`pending` (400), `session_user_facts` (200), `artifacts_referenced` (300).

`covers_through_seq` is derived deterministically by the skill postprocessor
and the result is appended as a `role: "summary"` entry in `history.jsonl`.

## Configuration (`reyn.yaml`)

```yaml
chat:
  compaction:
    trigger_total_tokens: 30000
    head_size: 12
    tail_size: 12
    body_token_cap: 1500
    min_compact_batch: 5
    section_token_caps:
      topic_arc: 200
      decisions: 400
      pending: 400
      session_user_facts: 200
      artifacts_referenced: 300
```

## Trade-offs

**Preserved:** topic arc, decisions, pending items, user facts, referenced
artifacts (= including tool activity — files read /
URLs fetched / MCP tools called surface as `artifacts_referenced`
entries when the result is conversation-relevant), and the raw first/last
N turns.

**Lost:** verbatim phrasing of compacted turns; exact ordering of minor
exchanges. Section caps are soft — slight overruns self-correct on the
next compaction pass.

### Tool-aware compaction

`new_turns` includes `role="assistant"` entries with `tool_calls` and
`role="tool"` response entries (= what the router_loop emitted during
the user turn, persisted via `RouterLoopHost.append_history_entry`).
The compactor sees these as structured input and decides whether to
record the call under `artifacts_referenced`. Tool turns count toward
the head/tail/body slice the same as plain conversational turns.

Compaction runs in a background asyncio task and never blocks the current
turn. Events `compaction_started` / `compaction_completed` /
`compaction_failed` are emitted to the session event log (P6).

## See also

- `src/reyn/stdlib/skills/chat_compactor/`
- `src/reyn/chat/services/compaction_controller.py`
- [Events](../runtime/events.md)
