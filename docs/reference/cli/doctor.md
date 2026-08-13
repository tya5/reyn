---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn doctor]
---

# `reyn doctor`

Report **measured** health, never **declared** health (#4364). Read-only — this
command never deletes, writes, or repairs anything; every line it prints comes from
actually reading a live effect (a file's real age/size on disk), not from restating a
config value back.

This is PR-3a of a staged arc — see [`.reyn/` directory layout](../runtime/reyn-dir-layout.md)
and the design constraints below for what's in and out of scope today.

## Synopsis

```
reyn doctor [--project-root <path>]
```

`--project-root` defaults to the current directory; it must contain a `.reyn/` tree
(the same resolution `reyn chat` and other project-scoped commands use, walking up
from the given path to find `reyn.yaml`).

## Design constraints (why the output reads the way it does)

- **Measure, don't assert.** "A hook is registered" is `reyn config show`'s job;
  "a hook's argv actually launched" is a separate measurement, landed since as
  C-1 (#4596 — see the hook launch-probe section below). The disk-usage checks
  follow the same rule: every number comes from `os.stat`/`Path.rglob`, never
  from re-reading a config object.
- **Report-only, never mutate.** `doctor` reaches into wider internals than any
  other CLI surface (sandbox / MCP / hook, in later slices). Every local-CLI
  precedent surveyed for this design (`npm doctor`, `claude doctor`,
  oh-my-opencode `doctor`) reports-only or requires explicit confirmation before
  acting; none auto-repairs from a CLI invocation. `reyn doctor` never calls a
  delete/purge function — only read-only queries.
- **Disclose what wasn't measured, and how "measurable" was decided.** A doctor
  that only ever prints what it happens to check reads, on a clean run, identical
  to a doctor that checked everything — a shape this repo hit repeatedly the night
  this design was settled (a declaration outrunning what was actually verified).
  The first line of output is therefore always a coverage disclosure: total config
  leaves / how many have a measurable effective surface / how many don't, with the
  measurability criterion printed alongside the count — never a bare "N checks
  passed."

## What this PR checks

```bash
$ reyn doctor

154 config leaves total, 2 have a measurable effective surface (checked below), 152 uncovered.
  Measurable means: a leaf counts as measurable here iff this module reads its LIVE EFFECT
  (a file's actual age/size on disk), not merely the config value itself -- re-reading the
  config object back is not a measurement

Disk usage — declared retention vs. actual (.reyn/events/):
  declared: cleanup_period_days=30 (0=disabled), max_disk_usage_percent=10.0 (0=disabled)
  actual:   1 file(s), 22 bytes, oldest = 43 day(s) old
  ⚠ 1 file(s) currently exceed the declared policy but have not been purged yet (purge
  fires on the next write/rotation, not continuously) — run 'reyn events purge' to apply
  the policy now if you don't want to wait.

Disk usage — no declared retention policy (visibility only):
  media/:         0 file(s), 0 bytes
  tool-results/:  0 file(s), 0 bytes
  history.jsonl:  0 file(s), 0 bytes, 0 turn(s)
  total (media+tool-results+history): 0 bytes
  (filesystem free space: 243,848,552,448 bytes)
```

### `.reyn/events/` — declared vs. actual

Compares `audit_events.cleanup_period_days` / `audit_events.max_disk_usage_percent`
(the automatic-purge policy, [#4479](https://github.com/tya5/reyn/issues/4479)) against
the real on-disk file count, byte total, and oldest file's age. The `select_purge_targets`
query this reuses (`reyn.core.events.event_purge`) is the SAME read-only selection logic
`reyn events purge` and the automatic trigger both use — no re-derived logic here.

A non-empty "exceed the declared policy" finding means files exist RIGHT NOW that the
policy's own axes would purge, but haven't been yet — the automatic trigger only fires
on write/rotation, not continuously, so a quiet period can leave a real backlog this
command surfaces without waiting for the next write.

### `media/` / `tool-results/` / `history.jsonl` — no declared policy (visibility only)

None of these three has a retention policy yet ([#4478](https://github.com/tya5/reyn/issues/4478) /
[#4476](https://github.com/tya5/reyn/issues/4476) Phase 2 unimplemented) — the visibility
itself is the finding: an unowned, unbounded resource made visible rather than silently
absent from every report. Reuses `MediaStore.storage_stats()` and
`aggregate_history_stats()`, the same functions [`reyn storage stats`](storage.md) reports.

## Out of scope for this PR (later slices, same arc)

- **C-2** (zero-responder subscription detection) — still a later slice.

  **C-1 landed in #4596** and is no longer out of scope. It is a *differential*
  probe, not an exec check: the backend's own known-good control binary is
  launched under the hook's policy first, and only the difference between that
  and the hook's `argv[0]` is reported (`ok` / `target_failed` / `sandbox_failed`,
  or `None` where the resolved backend cannot probe at all — Noop, or Docker,
  whose image contents reyn cannot assume). It launches only `argv[0]`, never the
  hook's configured args, and its output is labeled a launch probe rather than a
  run (owner ruling: running a hook's real configured args as a side effect of
  `reyn doctor` would make the diagnostic itself a footgun). A program that
  *requires* arguments therefore reports here without being broken — disclosed in
  the output rather than papered over by passing the args.
- **C-5 / C-6** (sandbox posture, listen port, and model-name declared-vs-effective
  pairs) — each needs its own new measurement code (reading the resolved sandbox
  backend object, introspecting a live bound socket, a real litellm probe call),
  unlike this PR's C-7 disk checks, which reuse existing measurement functions.

## Related

- [`reyn storage`](storage.md) — the measurement functions this command's
  no-declared-policy section reuses
- [`reyn events`](events.md) — `reyn events purge`, the manual counterpart to the
  automatic policy this command's events section checks compliance against
- [`.reyn/` directory layout](../runtime/reyn-dir-layout.md) — the audit-bucket
  classification for `media/`/`tool-results/`/`events/`
