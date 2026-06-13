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

This opens an interactive checkpoint timeline in the TUI. Each row shows:

| Column | Description |
|--------|-------------|
| seq | Global sequence number of the checkpoint |
| time | Timestamp when the checkpoint was created |
| kind | Boundary type: `turn` / `plan-step` / `phase` |

Navigate with **↑ / ↓**, select with **Enter**. Press **Esc** to close without rewinding.

**Shortcut**: double-tap **Esc Esc** from anywhere in the TUI to open the picker directly.

## Rewind to a specific seq

```
/rewind <N>
```

Rewinds directly to seq N without opening the picker. Both the conversation state and workspace files are restored to their state at seq N.

## Navigate the branch tree

After any rewind or fork, the picker switches to a **tree view** showing all branches. Each branch is labeled with its anchor checkpoint. Use ↑ / ↓ + Enter to select a checkpoint on any branch — active or abandoned.

- Selecting a seq on the **current branch**: undo (rewinds the current branch).
- Selecting a seq on an **inactive branch**: fork-switch (activates that branch).

## Container mode

When using the container environment backend (`--container`), the workspace
rewind operates on the container filesystem via shadow-git `as-of-N` — the same
user experience applies; the substrate is container-side.

## Pending features

| Feature | Status |
|---------|--------|
| `/rewind` with in-turn edit (`ctrl+t`) to create a new fork-and-edit branch | ✅ Phase 2c, landed |
| Retention window config (`retention: keep_generations: N`) to GC old checkpoints | ⏳ ADR-0038 Stage 1e — designed, not yet wired |
| `/rewind` picker over WebSocket / A2A web surface | ⏳ Phase 2d, in-progress |

## See also

- [Time-Travel concepts and architecture](../../concepts/runtime/time-travel.md)
- [Crash recovery and skill resume](../../concepts/skills/skill-resume.md) — different from rewind; automatic, forward-only
