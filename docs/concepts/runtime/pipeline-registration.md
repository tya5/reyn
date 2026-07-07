---
type: concept
topic: runtime
audience: [human, agent]
search_hints: [pipeline registration, pipelines entries, register pipeline, run_pipeline, pipeline DSL, PipelineRegistry, pipeline__run, call step target, load pipeline from config, add a pipeline, pipeline_management, install a pipeline]
---

# Pipeline registration

A **pipeline** is a deterministic, multi-step control flow written in the
pipeline DSL (a single YAML document). Once registered, an agent can launch
it by name with `run_pipeline` (or the catalog verb `pipeline__<name>`), and
one pipeline can `call` another by name.

## Registration: explicit entries, no directory scan

Pipelines are registered purely via `pipelines.entries` declarations in
config — the same explicit-registration model as `skills.entries` /
`mcp.servers`. There is no directory scan; a pipeline DSL file sitting on
disk with no config entry is invisible to every session.

```yaml
# reyn.yaml
pipelines:
  entries:
    hello:
      path: pipelines/hello.yaml
      description: "Minimal greeting pipeline"
      enabled: true
```

## Adding a pipeline

1. Write one Appendix-B DSL document per `*.yaml` file. Each file declares its
   name with a top-level `pipeline:` key:

   ```yaml
   # pipelines/hello.yaml
   pipeline: hello
   description: Minimal greeting pipeline.
   steps:
     - transform: {value: "'Hello, ' + ctx.name + '!'", output: greeting}
   ```

2. Declare a `pipelines.entries.<key>` entry pointing at the file (see above).
   **The entry key must match the DSL's own declared `pipeline:` name
   exactly** — see below.

3. Start (or restart) the session. The pipeline is now registered under its
   declared name and an agent can launch it:

   ```
   run_pipeline(name="hello", input={name: "Reyn"})
   ```

   or via the qualified catalog verb the action catalog surfaces:

   ```
   pipeline__hello({name: "Reyn"})
   ```

## The declared name is authoritative

A pipeline registers under the name in its `pipeline:` key. This is the
identity a `call` (or `match`) step's `pipeline:` target resolves against, so
it must be exact. Unlike `skills.entries` (where the config key freely names
the skill), **the `pipelines.entries` key must match the DSL's declared name
exactly** — a mismatch fails session start loudly, naming both the key and
the declared name. A file's own filename is irrelevant either way; only the
`path` it's referenced from and the `pipeline:` key inside it matter.

## Config cascade

`pipelines.entries` merges across the same tiers as every other config
section, later tiers winning on name collision:

1. `~/.reyn/config.yaml` — user-global
2. `reyn.yaml` — project
3. `reyn.local.yaml` — project-local (gitignored)
4. `.reyn/config/pipelines.yaml` — runtime-dynamic, written by the
   `pipeline_management__install_*` tools

Hand-editing any of the first three is a normal way to register a pipeline;
the fourth is written automatically by the install tools below and reflects
what a session installed for itself.

## Failure behavior — per-entry isolated, visible but non-fatal

Loading is per-entry isolated: a broken declaration is never silently
dropped, but it also never takes down the rest of the session's pipelines
(or the session itself). At session-factory time (every `reyn chat` / `reyn
web` startup), a broken entry is caught, logged as a warning, durably
recorded as a `pipeline_load_failed` event (readable via
`scripts/dogfood_trace.py` / the raw `.reyn/events/direct/cli/*.jsonl`
files), and skipped — every other declared entry still loads and registers
normally:

| Condition | Behavior |
|-----------|----------|
| Malformed DSL file | That entry is skipped; logged + durably recorded, naming the offending file. Other entries still load. |
| Entry key ≠ the DSL's declared `pipeline:` name | That entry is skipped; logged + durably recorded, naming both the key and the declared name. Other entries still load. |
| Two entries declaring the same `pipeline:` name | The FIRST-registered entry (config declaration order) wins; the later, colliding entry is skipped and logged + durably recorded. |
| An entry's `path` does not exist | That entry is skipped; logged + durably recorded, naming the path. Other entries still load. |
| No `pipelines.entries` declared | No pipelines registered (empty registry) — not a failure, nothing logged. |

This is a deliberate middle ground between two failure postures neither of
which fit: fully silent (a typo could vanish a pipeline the operator meant to
ship, with zero trace — the earlier design's own stated reason for
fail-loud) and fully fatal (the original fail-loud design — the first
broken entry anywhere in `pipelines.entries` used to crash the ENTIRE
session, which meant one unrelated pipeline's typo could take down `reyn
chat` / `reyn web` startup entirely). Per-entry isolation keeps a broken
entry visible to the operator (warning + durable event) while letting every
healthy entry — and the session itself — start normally.

The hot-reload seam (`/reload`, `Session._reapply_pipelines`) is the one
exception: it opts back into the OLD atomic, fail-loud posture (any broken
entry aborts the WHOLE rebuild, leaving the previously-loaded registry
fully intact) — a live session's already-running pipeline registry should
never have an entry silently vanish out from under it mid-reload, so a
broken edit at reload time is rejected wholesale rather than partially
applied.

## Installing pipelines

Two chat-callable tools under the `pipeline_management` category write
`pipelines.yaml` entries — there is no `reyn pipeline` CLI equivalent
(pipeline management is a chat-driven, in-conversation flow, mirroring
`skill_management`).

### `pipeline_management__install_local`

Registers a local pipeline DSL file into `.reyn/config/pipelines.yaml`:

1. Parses the DSL file at the given path (validation step — a malformed file
   is refused).
2. Resolves the registration name from the DSL's own declared `pipeline:` key;
   an optional `name` argument must match it exactly or the install is refused.
3. Threat-scans the description (strict scope) — blocks on a blocking-severity
   match.
4. Gates the `pipelines.yaml` write through the standard `require_file_write`
   permission flow.
5. Writes the entry, records a config generation (crash-recovery — survives
   WAL truncation), emits a `pipeline_installed` P6 event, and requests a
   hot-reload.

### `pipeline_management__install_source`

Fetches a pipeline from a git/GitHub URL and installs the clone:

1. Gates `require_http_get` for the source host.
2. Shallow-clones the repo (`--depth 1`) to `.reyn/pipelines/<name>/`. A
   `//subdir` suffix on the URL (mirroring Terraform's module-subdir
   convention) selects a subdirectory of the clone instead of its root.
3. Locates the DSL file in the clone — an explicit `path` argument selects it
   when the repo/subdir contains more than one `*.yaml` file — then proceeds
   through the same parse → name-validate → threat-scan → gate → write →
   hot-reload pipeline as the local path, with the registered `path` pointing
   at the installed copy.

**Path-safety hardening** (both tools, since the resolved name feeds a
filesystem path under `.reyn/pipelines/`): the derived name — from the
`name` argument or the DSL's declared `pipeline:` name — is rejected outright
unless it is a single safe path component (`[A-Za-z0-9._-]+`, no `..`, no
leading dot, no separators). A belt-and-suspenders containment check
(`resolve()` + `relative_to()`) additionally refuses any install destination
that would resolve outside `.reyn/pipelines/`, guarding against a gap in the
name check itself. Neither check silently rewrites an unsafe name —
installation is refused with an explicit error instead.

## Hot-reload

Edits to `.reyn/config/pipelines.yaml` (or to `pipelines.entries` in
`reyn.yaml` / `reyn.local.yaml`) take effect at the next turn boundary via the
`"pipelines"` reload seam — no session restart needed. See
[Concepts: Config hot-reload](config-hot-reload.md).

## Security — launching a pipeline stays gated

Registering a pipeline does **not** loosen the capability floor. Launching a
pipeline (`run_pipeline` / `run_pipeline_async` / the inline launch verbs, and
their `pipeline__*` catalog forms) is on the same restricted floor as spawning a
sub-session or re-delegating: a pipeline step can itself write, execute, or
delegate, so a pipeline launch is a cost-bound multi-step dispatch.

As a result, a context narrowed by the `_untrusted` floor (untrusted external
content is live) or the `_delegate` floor (an unbound delegate under
`delegation.capability_default=deny`) **cannot launch a pipeline**, whether or
not one is registered. Loading a pipeline definition makes it available to
authorized agents; it never creates a bypass of those floors. See
[Capability profiles](capability-profile.md) and
[Delegation policy](delegation-policy.md).

The `pipeline_management__install_*` verbs (the REGISTRATION action itself,
distinct from launching) sit on the same untrusted-content / unbound-delegate
floor as `skill_management__install_*` and `mcp__install_*` — no registering a
pipeline from untrusted content either.

## See also

- [Reference: `reyn.yaml`](../../reference/config/reyn-yaml.md) — `pipelines:` block schema
- [Concepts: Skills](../tools-integrations/skills.md) — the analogous explicit-registration + install-tool model
- [Concepts: permission model](permission-model.md) — the file-write/http-get gates the install tools use
- [Concepts: Config hot-reload](config-hot-reload.md) — the general reload cycle
- [Concepts: Pipelines](pipelines.md) — the execution model (driver-session, crash recovery, DSL primitives)
