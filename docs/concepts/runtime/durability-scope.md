---
type: concept
topic: architecture
audience: [human, agent]
---

# Crash-recovery completeness and the durability boundary

Reyn's promise is **complete crash-recovery of file content that lands inside the
workspace tree**, together with the runtime substrate (the event/WAL log and
runtime snapshots). A crash at any point recovers that state completely — no
half-applied step, no silently lost work. This page states exactly what the
promise covers and where its boundary is.

## Why a boundary, not "the whole filesystem"

Guaranteeing recovery for *every* byte a process might touch is not achievable: a
skill can shell out, a subprocess can write anywhere it has rights, an external
tool can mutate files Reyn never sees. A system that claimed to recover all of
that would be claiming something it cannot deliver.

So Reyn draws an **explicit boundary** instead, and the boundary is
**workspace-tree membership** — does the written file live inside the workspace
tree? State inside the tree is recoverable; everything else is best-effort. The
promise is *precise* rather than aspirational — **complete within a stated
boundary** is a guarantee you can build on; "we try to recover everything" is not.

## Two layers: tracking vs. content-recovery

It helps to separate two distinct things Reyn does, because they have different
scopes:

- **L1 — per-mutation tracking.** Does Reyn emit an event *for each write*? This
  covers **Control IR file ops only** (`file.write` / `file.edit` /
  `file.delete` → a `workspace_updated` audit event). L1 is **audit-only** — it
  records that a mutation happened; it is *not* the recovery mechanism.
- **L2 — file-content recovery.** Can Reyn restore the actual bytes after a
  crash? This is the **whole workspace tree, captured by shadow-git** at each
  generation cut (a `git add -A` over the entire work-tree, committed and tagged
  `reyn-gen-<seq>`). L2 captures a file **regardless of how it was written** — as
  long as it lives in the tree.

**The recovery boundary is L2 — tree membership.** L1 (per-mutation tracking) is
strictly narrower (Control IR only). A file can be fully recoverable (L2) without
being individually tracked (L1).

## Inside the boundary — content-recoverable

Writes whose result lands in the workspace tree, so L2 captures them:

- **Control IR file ops** — permission-gated, L1-tracked, **and** L2-captured.
  Fully covered: tracked per mutation and recoverable by content.
- **`sandboxed_exec` writes inside the workspace tree** — with `cwd` at the
  workspace base dir and relative paths, the output lands in the tree. **L2
  recovered, but L1 untracked** (no per-write event — recovery is by tree
  capture, not by an audit record).
- **Container-backend work-tree writes** — captured at L2 (the runner runs
  shadow-git on the container side of the work-tree).

## Outside the boundary — best-effort, not recovered

Writes whose result does **not** land in the workspace tree, so tree capture
never sees them:

- **Writes outside the workspace tree** — absolute paths such as `/tmp` or
  `$HOME` from `sandboxed_exec` or unsafe-mode Python.
- **MCP / external-process filesystem access** — external by construction, so
  outside the tree capture entirely.
- **Noop-sandbox platforms** — on platforms without an enforcing sandbox
  (anything other than macOS Seatbelt / Linux Landlock), arbitrary-path writes
  are not isolated ("no isolation enforced"), so nothing constrains them into the
  tree.

These may survive a crash, but Reyn makes **no completeness promise** about them —
the tree capture holds no copy to restore. Work that must be recoverable should
land inside the workspace tree.

**Not a bypass (safe by construction):** the safe-mode `python` op and the
CodeAct op cannot write to the filesystem at all — `open` and `subprocess` are
banned — so they create no outside-boundary writes.

### A note on enforcement precision

The boundary's *essence* is tree-membership. The exact mechanics that keep writes
inside the tree — host vs. container sandbox enforcement consistency, the default
`write_paths`, and the precise filesystem reach of MCP servers — are
backend- and platform-dependent enforcement details. Treat the tree-membership
boundary as the durable contract, and the per-backend enforcement as the
mechanism that upholds it to varying precision, rather than assuming uniform
enforcement everywhere.

## Current state: the runtime / workspace durability asymmetry

Being honest about where the boundary's *durability* stands today, distinct from
its *scope*:

- **Runtime substrate (WAL + snapshots)** is **power-loss durable** — the
  fsync-per-append contract means an OS crash or a power cut loses nothing that
  was committed.
- **Workspace content recovery (L2)** is **not yet fsync-ordered** — on a hard
  power loss the captured tree can end up diverged from the runtime state it
  should pair with.

This asymmetry is **in progress**, not a settled design choice. The direction is
to bring L2 under a durability + ordering barrier that mirrors the WAL, so the
entire inside-boundary set is power-loss durable, not just the runtime half.
Until that lands, the completeness guarantee holds for clean process crashes; the
power-loss edge for workspace content is the gap being closed. The boundary
principle does not change — only how far its durability currently reaches.

## See also

- [Events](events.md) — the WAL and its fsync-per-append durability contract
- [Time-travel](time-travel.md) — reconstruct over the WAL + runtime snapshots, and the shadow-git generations behind L2
- [Workspace](workspace.md) — the workspace tree itself
- [Permission model](permission-model.md) — the gate over Control IR file ops (L1)
- [Crash recovery / skill resume](../skills/skill-resume.md) — the recovery mechanism
