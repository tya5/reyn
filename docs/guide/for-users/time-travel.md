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

## Web edit (Phase 2d)

When using Reyn through the web interface (WebSocket / A2A), `/rewind` opens the same checkpoint picker. After selecting a checkpoint to branch from, the web edit flow presents the original message for you to retype your edited version and submit — inline prefill is not supported in the web surface, so you enter the replacement text directly. Submitting creates a new fork from the rewound checkpoint.

## Container mode

When using the container environment backend (`--container`), the workspace
rewind operates on the container filesystem via shadow-git `as-of-N` — the same
user experience applies; the substrate is container-side.

## Tuning time-travel cost

Time-travel is on by default. Its largest constant cost is the **workspace shadow-git capture** that runs at every checkpoint boundary (every turn and plan-step) so a rewind can restore your repo files. You can tune this in `reyn.yaml` under the `time_travel` block. For the full per-key reference see [`reyn.yaml` § time_travel](../../reference/config/reyn-yaml.md#time_travel-block); for *why* it costs what it does, see [Cost and the runtime-only opt-out](../../concepts/runtime/time-travel.md#cost-and-the-runtime-only-opt-out).

### Opt out of file rewind (`workspace_capture: false`)

```yaml
# reyn.yaml
time_travel:
  workspace_capture: false   # default is true
```

This selects **runtime-only rewind**: `/rewind`, the fork picker, branching, and checkout all still work and restore your **agent + conversation state**, but the **working tree is left untouched** (repo files are not snapshotted or rewound).

When it's worth it:

- your workspace is **large** — the per-boundary `git add -A` stats the whole tree;
- you run in a **container** — each capture is a `docker exec` round-trip;
- you only ever rewind to **re-run from a past point**, never to inspect the repo *as it was* there.

This is the same fidelity act-turn rewind already gives ("re-run from here", not "the repo as it was here"). Crash recovery is **unaffected** — it rides the WAL, not the workspace capture. The setting is **read at startup** (run-level), not a mid-session toggle.

### Opt in to per-step capture (`act_turn_capture: true`) — advanced

```yaml
# reyn.yaml
time_travel:
  act_turn_capture: true   # default is false
```

This adds a cheaper **per-op** workspace snapshot (one per skill-run step, not just per turn/plan-step). It is **off by default** and **high-frequency** (one capture per op).

**Honest status**: enabling it today gives **no user-visible rewind benefit** — the picker does not yet expose a way to land a rewind *mid-skill-run*, so the extra per-step snapshots are foundational groundwork for a future capability, not something you can currently navigate to. Leave it off unless you specifically want that groundwork captured ahead of time. It is also a **no-op when `workspace_capture: false`** (the per-step capture rides the same workspace store).

## Pending features

| Feature | Status |
|---------|--------|
| `/rewind` with in-turn edit (`ctrl+t`) to create a new fork-and-edit branch | ✅ Phase 2c, landed |
| Retention window config (`retention: keep_generations: N`) to GC old checkpoints | ⏳ designed, not yet wired |
| `/rewind` picker over WebSocket / A2A web surface; web edit via `AskUserMessage` UX | ✅ Phase 2d, landed |

## See also

- [Time-Travel concepts and architecture](../../concepts/runtime/time-travel.md)
- [Crash recovery and skill resume](../../concepts/skills/skill-resume.md) — different from rewind; automatic, forward-only
