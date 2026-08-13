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

155 config leaves total, 2 have a measurable effective surface (checked below), 153 uncovered.
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

Hook launch probe (argv[0] only, no configured args — a launch
probe, not a run; D-2: doctor never executes a hook for real):
  no exec/exec_capture hooks configured

External-event producer/consumer pairing (a producer with 0
subscribing hooks is a real gap; a point with no producer is not
reported — see 'not checked' below for why some points aren't):
  ✗ file_changed: producer present (1 declared fs_watch path(s)) but 0 subscribing hooks — this point's notifications have nowhere to go
  ? mcp_resource_updated: no event history yet
  ? webhook_received: no event history yet
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

### Hook launch probe (C-1)

A *differential* probe, not an exec check: the backend's own known-good control binary
is launched under each `exec`/`exec_capture` hook's own sandbox policy first, and only
the difference between that and the hook's `argv[0]` is reported (`ok` / `target_failed`
/ `sandbox_failed`, or a `?` line where the resolved backend cannot probe at all — Noop,
or Docker, whose image contents reyn cannot assume). It launches only `argv[0]`, never
the hook's configured args, and its output is labeled a launch probe rather than a run
(owner ruling: running a hook's real configured args as a side effect of `reyn doctor`
would make the diagnostic itself a footgun). A program that *requires* arguments
therefore reports here without being broken — disclosed in the output rather than
papered over by passing the args.

### External-event producer/consumer pairing (C-2)

For each of the 4 external ingress points (`hooks.schema_registry._EXTERNAL_POINTS`:
`file_changed` / `cron_fired` / `mcp_resource_updated` / `webhook_received`), checks
whether a PRODUCER exists and, only where one does, whether any hook is registered to
consume it (`reyn.yaml`'s top-level `hooks:` + `.reyn/config/hooks.yaml` + every
`.reyn/agents/<name>/hooks.yaml`, the SAME 3-layer combine `Session._build_hook_registry`
uses at runtime — a malformed layer is dropped, its siblings kept, so one bad file
cannot hide a real gap).

The check pairs producer↔consumer, not subscription↔consumer — an architect design
correction mid-arc: an MCP resource subscription is only meaningful on a HELD
(persistent) connection, so `reyn doctor` (a separate, one-shot process) cannot observe
one directly. Producer evidence differs per point:

- `file_changed` — a declared `fs_watch.paths` entry.
- `cron_fired` — an *enabled* `cron.jobs[]` entry (a disabled job is not a producer).
- `mcp_resource_updated` / `webhook_received` — past evidence in `.reyn/events` (both
  kinds ARE audit-events, `event_schema.py` — `webhook_received` gained its own kind in
  #4618, joining this check in #4620), scanned newest-first, bounded to the most recent
  20 dated files (a kind lookup has no index — `.reyn/events` is append-only — so this
  bound keeps a "did it ever happen" query cheap even with retention disabled; the
  question only needs "at least once," so an early exit can never turn a real positive
  into a false negative). **This check is windowed, unlike the other two** (#4614): "not
  seen in the newest 20 files" is NOT proof of "no producer" — a real producer whose last
  arrival predates the window is indistinguishable from one that never fired. Reporting
  nothing here (the pre-#4614 behavior) silently hid exactly the state C-2 exists to
  catch, so both points ALWAYS print a line — `✓`/`✗` when seen within the window, or
  `? <point>: not seen in the newest N event file(s) scanned — ... NOT proof no producer
  exists` otherwise — never folded into the "no producer → no finding" rule below.
  **#4624 exception**: when `.reyn/events` has NO dated files at all (a fresh install, or
  retention already purged everything), N is 0 and "a producer whose last arrival predates
  the window" cannot be true — there is no window to predate. The #4614 caveat would be a
  TRUE but EMPTY statement there, and an empty caveat printed on every fresh install trains
  the reader to skip caveats (architect's finding, #4622 co-vet), so this one case prints
  the plain fact instead: `? <point>: no event history yet`, no window talk.
  `webhook_received` has no config surface of its own (unlike `file_changed`/`cron_fired`),
  so it can ONLY ever be evidence-based here — there is no complete-read alternative for it.

A point with a COMPLETE producer read (`file_changed`/`cron_fired`) and no producer
prints no finding at all — reporting "0 subscribing hooks" for
a point that will never fire would be noise, not signal.

### Sandbox posture — declared vs. RESOLVED (C-5)

```
Sandbox posture — declared vs. RESOLVED (absence of a declaration
does not mean unrestricted; see the resolved backend below):
  declared: sandbox.backend='auto', sandbox.on_unsupported='warn'
  declared: no sandbox.policy — NOT the same as unrestricted, see resolved backend below
  resolved: 'seatbelt' (production's own resolution — a backend that cannot enforce is already treated as absent at this step, #2983, so this name IS the enforcement witness)
```

The motivating real case (architect's own note, #4364): an operator read "no
`sandbox.policy` declared" as "unrestricted," but the resolved backend was actually
enforcing all along (`SeatbeltBackend`, `write_paths=[]`) — declaration and
resolution silently disagreed, and only the resolved side is true.

`reyn doctor` reports declared `sandbox.backend` / `sandbox.on_unsupported` /
`sandbox.policy` (from config, verbatim — only the write-scope keys,
`allow_write_paths`/`deny_write_paths`, are echoed) next to the backend
production's OWN resolution (`reyn.security.sandbox.launcher.resolve_backend`
— the SAME call C-1's hook probe already makes) actually hands back. This is
deliberately **not** a second, doctor-invented probe: `get_default_backend()`
already self-tests a real deny before returning a backend (#2983) and applies
`sandbox.on_unsupported` to any backend that can't enforce, so "which backend
resolved" already carries the enforcement witness — doctor reports that
verdict, never re-derives it. If the declared backend was explicitly forced
(not `'auto'`) and the resolution fell back to a different one, the line says
`DOWNGRADED from declared '<name>'` rather than silently agreeing with the
fallback. If `sandbox.on_unsupported: error` makes resolution itself
fail-closed, doctor reports `resolved: refuses to run (...)` rather than
crashing or swallowing the error.

`reyn doctor` has no op context (no LLM tool call it's diagnosing), so it does
NOT build a `resolve_sandbox_policy()` call — that needs a caller-supplied
`write_paths` floor ("this op needs this directory") doctor cannot know and
must not invent a stand-in for. Only the declared `sandbox.policy` dict's own
write-scope keys are shown, never a merged/resolved policy.

## Out of scope for this PR (later slices, same arc)

- **C-6** (listen port and model-name declared-vs-effective pairs) — each needs
  its own new measurement code (introspecting a live bound socket, a real
  litellm probe call), unlike C-1/C-2/C-5/C-7, which reuse existing measurement
  functions.

## Related

- [`reyn storage`](storage.md) — the measurement functions this command's
  no-declared-policy section reuses
- [`reyn events`](events.md) — `reyn events purge`, the manual counterpart to the
  automatic policy this command's events section checks compliance against
- [`.reyn/` directory layout](../runtime/reyn-dir-layout.md) — the audit-bucket
  classification for `media/`/`tool-results/`/`events/`
