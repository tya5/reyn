---
type: concept
topic: architecture
audience: [human, agent]
---

# Crash-recovery completeness and the durability boundary

Reyn's promise is **complete crash-recovery of Reyn-mediated state**. Any state
change that flows through Reyn's mediation layer is made durable *before the run
proceeds*, so a crash at any point recovers completely — no half-applied step, no
silently lost work. This page states exactly what that promise covers, and where
its boundary is.

## Why a boundary, not "the whole filesystem"

Guaranteeing recovery for *every* byte a process might touch is not achievable: a
skill can shell out, a subprocess can write anywhere it has rights, an external
tool can mutate files Reyn never sees. A system that claimed to recover all of
that would be claiming something it cannot deliver.

So Reyn draws an **explicit boundary** instead. State that passes through Reyn's
mediation is fully recoverable; everything else is best-effort. The promise is
*precise* rather than aspirational — **complete within a stated boundary** is a
guarantee you can build on; "we try to recover everything" is not.

## Inside the boundary — mediated, recovery-guaranteed

The defining property of "inside" is **mediation**: the state change passes
through Reyn's own machinery — Control IR, the permission gate, and the event
log — so the OS has a record of it and can replay or restore it. Three substrates
make up the mediated set:

- **Events / WAL** — every state change appends to the write-ahead log, made
  durable per append (synchronous fsync) before the run continues. This is the
  spine of recovery. See [Events](events.md) for the WAL vs audit-event
  distinction.
- **Runtime snapshot** — conversation and run state is snapshotted at checkpoint
  boundaries and paired with the WAL for point-in-time reconstruct. See
  [Time-travel](time-travel.md).
- **Workspace artifacts** — files written through Control IR `file.*` ops, gated
  by the [permission model](permission-model.md). Because every workspace
  mutation goes through that permission-checked, event-logged channel, the
  workspace is part of the recoverable boundary. See [Workspace](workspace.md).

> **Boundary extent.** The exact inventory of write paths that are mediated
> (guaranteed) versus those that bypass mediation (best-effort) is derived from a
> filesystem-mediation flow-trace of the running system. This page states the
> *principle* of the boundary; the per-path extent is mapped from that trace and
> kept in sync here.

## Outside the boundary — best-effort, not guaranteed

Filesystem access that does **not** go through Reyn's mediation is outside the
recovery guarantee. By category:

- Direct writes a tool or subprocess makes without going through a Control IR op.
- External files the workspace references but does not own.
- Anything that bypasses the permission-gated, event-logged channel.

Such writes may well survive a crash — but Reyn makes **no completeness promise**
about them, because by definition the OS holds no mediated record to replay. Work
that must be recoverable should flow through the mediated channel.

## Current state: the runtime / workspace durability asymmetry

Being honest about where the boundary's *durability* stands today, distinct from
its *scope*:

- **Runtime substrate (WAL + snapshots)** is **power-loss durable** — the
  fsync-per-append contract means an OS crash or a power cut loses nothing that
  was committed.
- **Workspace file substrate** is **not yet fsync-ordered** — on a hard power
  loss the workspace files can end up diverged from the runtime state they should
  pair with.

This asymmetry is **in progress**, not a settled design choice. The direction is
to **symmetrize workspace durability within the boundary** — bringing the
workspace under a durability + ordering barrier that mirrors the WAL — so the
entire inside-boundary set is power-loss durable, not just the runtime half.
Until that lands, the completeness guarantee holds for clean process crashes; the
power-loss edge for workspace files is the gap being closed. The boundary
principle does not change — only how far its durability currently reaches.

## See also

- [Events](events.md) — the WAL and its fsync-per-append durability contract
- [Time-travel](time-travel.md) — reconstruct over the WAL + runtime snapshots
- [Workspace](workspace.md) — the mediated artifact store
- [Permission model](permission-model.md) — the gate that mediates writes
- [Crash recovery / skill resume](../skills/skill-resume.md) — the recovery mechanism
