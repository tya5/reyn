---
type: guide
topic: time-travel
audience: [human]
---

# How to rewind and branch

Reyn's time-travel lets you rewind to a past checkpoint and optionally branch
from that point. This guide covers the available commands and UI.

For how it works under the hood, see [Time-Travel concepts](../../concepts/runtime/time-travel.md).

## Open the rewind picker

```
/rewind
```

This opens an interactive checkpoint picker in the TUI, listing the checkpoints on the current branch. Each row shows:

| Column | Description |
|--------|-------------|
| seq | Global sequence number of the checkpoint |
| time | Timestamp when the checkpoint was created |
| kind | Boundary type: `turn` / `plan-step` |
| anchor | The first line of the human prompt active at that checkpoint (truncated to 80 characters), when one is known — a hint for which checkpoint to pick, not the full conversation |

Navigate with **↑ / ↓**, select with **Enter** — selecting a row does exactly what typing `/rewind <seq>` for that row does. Press **Esc** to close without rewinding.

Over `--connect` (the remote client), the same checkpoints are printed as a plain text list instead: the picker is a local region and is not carried on the wire. Rewind from there with `/rewind <seq>`.

## Rewind to a specific seq

```
/rewind <N>
```

Rewinds directly to seq N without opening the picker. The agent's conversation state is restored to seq N. User workspace files remain at HEAD — Reyn time-travels its own `.reyn/` state only.

## Rewind vs fork-switch

`/rewind <N>` is a unified *checkout*, so the same command covers both directions:

- A seq on the **current branch**: undo (rewinds the current branch).
- A seq on an **inactive branch**: fork-switch (activates that branch).

The picker itself lists **current-branch** checkpoints only, so reaching a seq on an abandoned branch means passing it to `/rewind <N>` directly.

## Web edit (Phase 2d)

When using Reyn through the web interface (AG-UI SSE / A2A), `/rewind` opens the same checkpoint picker. After selecting a checkpoint to branch from, the web edit flow presents the original message for you to retype your edited version and submit — inline prefill is not supported in the web surface, so you enter the replacement text directly. Submitting creates a new fork from the rewound checkpoint.

## Pending features

| Feature | Status |
|---------|--------|
| `/rewind` with in-turn edit (`ctrl+t`) to create a new fork-and-edit branch | ✅ Phase 2c, landed |
| Branch **tree view** in the picker (checkpoints on abandoned branches, not just the current one) | ⏳ not yet wired — `/rewind <N>` already fork-switches to an inactive branch's seq |
| `Esc Esc` double-tap shortcut to open the picker | ⏳ not yet wired — open it with `/rewind` |
| `/rewind` picker over AG-UI SSE / A2A web surface; web edit via `AskUserMessage` UX | ✅ Phase 2d, landed |

Retention window config (`retention: keep_generations: N`) — considered, decided **not** to wire (#3987, 2026-08-11): the WAL/generation GC already runs correctly on the default live-only floor, throttled at every chat-turn boundary, with no unbounded-growth risk. The only thing a `retention:` config block would have added is a *deeper* undo window than the live floor — a capability nobody had asked for while it sat unconfigurable. `RetentionPolicy.from_config` was removed rather than left dead.

## See also

- [Time-Travel concepts and architecture](../../concepts/runtime/time-travel.md)

