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

### MCP servers — last negotiated version/capabilities (C-3(b))

```
MCP servers — last negotiated version/capabilities (audit-log
evidence, not a live probe; D-2: doctor never connects):
  ✓ filesystem: last negotiated '2025-11-25', capabilities=['resources', 'tools']
  ? brave: no event history yet
```

The motivating real case (architect's own note, #4364): a protocol-version mismatch
between reyn (2.0) and a connected server (1.27.1) had already fallen back silently
to an older shared version — nothing was BROKEN, so nothing raised, and the only way
to learn it happened was digging through the audit log after the fact. `reyn doctor`
now surfaces the same fact in one line.

For each MCP server declared under `mcp.servers`, this reuses the SAME windowed
evidence-based scan C-2's `mcp_resource_updated`/`webhook_received` checks already
use (`_mcp_initialized_evidence`, sharing `_MCP_EVENT_SCAN_MAX_FILES`) — the newest
`mcp_initialized` audit-event record per server (emitted once per real (re)connect,
`mcp/connection_service.py`) carries `negotiated_version` + `capabilities` verbatim.
`✓` when found within the window; `? <server>: no event history yet` when
`.reyn/events` has no dated files at all (#4624's fresh-install shape — the #4614
windowed caveat would be a true but empty statement there); `? <server>: not seen in
the newest N event file(s) scanned — ... NOT proof the server was never reached`
otherwise, same #4614 wording as the other windowed checks.

**C-3(a)** (an actual live `tools/list` connect + response check) was ruled
unnecessary: the evidence this check reports already exists from the connections
`reyn` itself made — a SEPARATE live reachability probe from doctor's own one-shot
process would duplicate work, and a held MCP connection is a session concept doctor
cannot observe directly anyway (the same architect correction C-2's own
producer↔consumer design rests on). D-2 holds: doctor never connects to a server
itself, only reads what a real connection already recorded.

### Model reachability — 0-token `GET {api_base}/v1/models` (C-4)

```
Model reachability — 0-token GET {api_base}/v1/models (never a
real completion call; D-2: doctor never spends inference cost):
  ✓ http://127.0.0.1:8998: reachable (HTTP 200)
    ✓ light ('gpt-4o'): accepted by the proxy's model list
    ✗ standard ('gpt-5.6-luna'): NOT in the proxy's model list — check the name form (bare vs 'provider/name')
```

The motivating real case (architect's own note, #4364): a configured model name
(`openai/gpt-5.6-luna`) that the LiteLLM proxy expected bare (`gpt-5.6-luna`) — no
error until the first real chat turn.

**The original ask was replaced, not implemented as first proposed** — a real litellm
completion probe would make `reyn doctor` itself charge the operator for inference,
exactly what the cross-cutting cost/budget band exists to keep OS-internal diagnostics
from doing. The 0-token `GET {api_base}/v1/models` answers the SAME two questions
(is `api_base` reachable, is the configured model name's form accepted) in one
request, at zero inference cost — reachability from the HTTP response itself (any
response, including 401/403, proves reachability; a connection error does not), model
acceptance from checking each declared `llm.models` entry's BARE name (stripped of the
`provider/` routing prefix reyn's own config uses) against the response's own model
list, when the response is a 200 carrying one.

Only `llm.api_base` (a LiteLLM proxy) is checked — a provider with no declared
`api_base` routes straight to its own hosted endpoint, which this module has no
per-provider URL table for and was not architect's motivating case (a local proxy).
`? not checked — no llm.api_base declared` when absent (D-3, same "cannot confirm"
shape `webhook_received` had before it gained a surface).

**API keys are never read** (litellm-boundary convention, owner's standing
instruction) — the request carries no `Authorization` header at all; a provider that
requires one to list models still proves reachability by responding (401/403 is a
real HTTP response, not a failure this check reports as unreachable).

Uses `reyn._network.build_sync_http_client` (the repo's own single httpx-client
constructor, #3075) — never a free-hand `httpx.Client(...)`, so this call site is
covered by the same standard-proxy-env/CA completeness gate every other reyn-owned
HTTP client is.

### Reyn process registry — how many, and whose ([#5226](https://github.com/tya5/reyn/issues/5226))

```
Reyn process registry (~/.reyn/processes/) — every reyn CLI
process currently alive on this machine, across every workspace:
  2 process(es) currently alive:
    pid=41213 ppid=1 started 2h 14m ago
      cwd:        /Users/alice/proj-a
      subcommand: chat
    pid=52098 ppid=41022 started 11d 3h ago
      cwd:        /Users/alice/proj-b
      subcommand: chat
```

A NEW category, not another declared-vs-effective pair: before this, reyn had no
way to answer "how many of ITSELF are alive, and whose" without an operator
manually reconstructing it via `ps`+`lsof -d cwd` — the motivating case (lead-
coder's own real machine, #5226) found 12 `reyn`/`reyn:chat` processes, 11 of
them abandoned, the oldest 11 days old.

Reads `reyn.runtime.process_registry.live_processes()` — a live read of PID-
keyed markers each reyn CLI process writes about ITSELF at startup
(`interfaces/cli/__init__.py:main()`, the same hook `set_process_title` uses),
never a process-table scan of its own (that's the OS's job, per lead-coder's
own ruling). Prints only the fields the marker itself carries — pid/ppid/cwd/
subcommand/age — never full argv or any path beyond cwd, mirroring
`reyn.runtime.proctitle`'s own stance against leaking more than the minimum
into anything read back after the fact.

**Report-only, D-2 unchanged**: no kill, no TTL expiry. Whether an abandoned
process should ever be reaped automatically is an owner-level judgment call
explicitly out of this slice's scope until the count is visible at all.

## Not applicable, measured (not a later slice)

C-6's "listen port declared-vs-effective" example does NOT apply to reyn's
own config surface (measured before writing any code, per lead-coder's
instruction, #4364): `reyn web`'s `--host`/`--port` are bare CLI arguments
(`interfaces/cli/commands/web.py`) with no corresponding `ReynConfig` field
anywhere — verified by walking the full schema
(`reyn.config.config_schema.walk_config_schema`) for any key naming a port;
there is none. A declared-vs-effective PAIR needs a DECLARATION to pair
against, and reyn's own port is set once, per-invocation, by the
operator's own CLI argument — there is nothing upstream of that argument
for doctor (a separate, later, short-lived process with no view into a
sibling `reyn web` process's argv) to compare it with. C-6's motivating
incident (a *different* project's `settings.port` silently going dead
across a dependency bump) illustrated the GENERAL "declared ≠ effective"
shape, never a claim that reyn itself declares a listen port. C-6's
GENERAL form is already implemented — C-5 above is architect's own named
special case of it, not a separate slice still owed. A regression guard
(`tests/interfaces/test_4364_c6_no_declared_listen_port.py`) fails the
moment a port-shaped config field appears, naming this section to revisit.

## Related

- [`reyn storage`](storage.md) — the measurement functions this command's
  no-declared-policy section reuses
- [`reyn events`](events.md) — `reyn events purge`, the manual counterpart to the
  automatic policy this command's events section checks compliance against
- [`.reyn/` directory layout](../runtime/reyn-dir-layout.md) — the audit-bucket
  classification for `media/`/`tool-results/`/`events/`
