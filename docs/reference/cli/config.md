---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn config]
---

# `reyn config`

Inspect and modify the effective Reyn configuration.

## Synopsis

```
reyn config [show]
reyn config fields
reyn config get <key>
reyn config set <key> <value>
reyn config validate
reyn config migrate [--dry-run]
reyn config migrate-mcp [--dry-run]
```

## Subcommands

| Subcommand | Description |
|------------|-------------|
| `show` | Print effective merged configuration as YAML (default). |
| `fields` | List all known keys with types and defaults. |
| `get <key>` | Print the value of a single dot-path key. |
| `set <key> <value>` | Write a key to `reyn.local.yaml`. Value is parsed as YAML. |
| `validate` | Report unrecognized/renamed keys (policy tier + the hot-reload IN-set) and known keys currently disabled by another key's value. Never fails the exit code — see [Notes](#notes). |
| `migrate [--dry-run]` | Rewrite renamed top-level config keys to their current location, in place, in whichever file each was found. Also reports (never auto-deletes) any REMOVED key with no successor — #4375. |
| `migrate-mcp [--dry-run]` | Move legacy `mcp.servers` entries out of `reyn.yaml`/`reyn.local.yaml`/`~/.reyn/config.yaml` into `.reyn/mcp.yaml` (#470 config separation). |

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success — including `validate` reporting findings (see [Notes](#notes)). |
| `1` | Unknown key (`get`/`set`), no project root found (`migrate-mcp`), or I/O error. |

## Notes

`reyn config set` always writes to `reyn.local.yaml` (gitignored) — never to `reyn.yaml`.

`reyn config validate` always exits `0`, even when it reports findings — it REPORTS, it
never gates (owner ruling: warn, never hard-fail, anywhere). It checks seven things,
each printed as its own labeled section (never merged into one list — the fix differs
per section, and merging would lose "which one do I fix, and how"). Note the middle
three checks (hook entries, MCP server placement, MCP transport type) each cover
multiple separate config *files* under one section, not one:

- **Unrecognized/renamed/removed keys, policy tier** (`reyn.yaml` / `reyn.local.yaml` /
  `~/.reyn/config.yaml`) — the same check `load_config`'s own startup warning runs.
  Fix: edit the file and restart, or run `reyn config migrate` for a renamed key with
  an automatic destination. A REMOVED key (#4375 — no successor exists, unlike a
  rename) is reported in its own "REMOVED ... delete them" section — `migrate` never
  auto-rewrites it (there is no destination) and never auto-deletes it either; the
  operator's next action is "delete this line", not "rewrite it".
- **Known keys currently disabled by another key's value** — the key is real and
  correctly spelled, but the current configuration makes it a no-op (e.g.
  `tool_use.universal_wrappers_enabled` has no effect while `tool_use.scheme`
  resolves to `enumerate-all` — both fields live under the same `tool_use:` block
  since #4552 PR-3+4 moved `universal_wrappers_enabled` out of the now-deleted
  `action_retrieval:` section). Each entry names the key it depends on and the fix.
- **Unrecognized/renamed/removed keys, the hot-reload IN-set** (`.reyn/{mcp,cron,hooks,skills,
  pipelines,presentations}.yaml`) — same unknown-key check, run against the merged
  IN-set instead of the policy tier. Fix: edit the `.reyn/*.yaml` file directly — it
  applies on the next turn automatically, no restart and no `reyn config migrate`
  support for this tier (a different remedy than the policy tier above, which is
  exactly why this is its own section).
- **Hook entry validation, all three real `hooks:` input paths** (#4501 / #4364
  PR-1) — the top-level unknown-key checks above only confirm `hooks:` itself is a
  recognized key; they never recurse into what each list *entry* contains. This
  section feeds every real hooks source through the real `load_hooks` parser
  (the SAME parser hook-loading itself uses) and reports any `HookConfigError`
  (e.g. an unrecognized or wrong-scope per-hook key), labeled by source:
    - **`reyn.yaml`'s own top-level `hooks:`** — the startup layer, and the one
      [the hooks guide](../../concepts/runtime/hooks.md) tells operators to write
      in. #4501 did not cover this source.
    - **`.reyn/config/hooks.yaml`** — the runtime IN-set (#4501's own original fix).
    - **every `.reyn/agents/<name>/hooks.yaml`** — the per-agent layer, one
      finding per agent directory that has a malformed entry.
  Fix: edit the offending hook entry in whichever labeled file the finding names.
- **MCP server placement** (#4631) — the same class of gap as hook entries above,
  for a different config surface: `unknown_config_keys` confirms `mcp:` itself is a
  recognized key but never opens what's under it, so a server entry written
  directly at `mcp.<name>` (instead of `mcp.servers.<name>`) loads WITHOUT error
  and WITHOUT warning — `cfg.mcp.servers` silently stays empty and the server is
  never registered. Detected by shape (a dict directly under `mcp:`, other than
  the real `servers` key, carrying `command`/`url`/`type` — a scalar config knob
  like `mcp.timeout_seconds` never takes that shape), checked PER SOURCE FILE
  (not the merged view the other checks above use) so the finding can name which
  file to fix: the same 3 static locations `migrate-mcp` already scans
  (`reyn.yaml` / `reyn.local.yaml` / `~/.reyn/config.yaml`) plus the dynamic
  `.reyn/config/mcp.yaml`. Fix: add the missing `servers:` key by hand —
  `migrate-mcp` relocates already-correctly-nested `mcp.servers` entries between
  files, but does not add a missing `servers:` key to a misplaced entry.
- **MCP transport type** (#4604) — reyn's own `mcp.servers.<name>.type` vocabulary
  renamed `"http"` to `"streamable-http"`, aligning with the Agent Plugins 1.0
  canonical `mcp.schema.json`. `MCPClient` already rejects the old value at
  connection time with a clear error naming the rename, but that only fires the
  next time the server is actually used — this check finds a stale `type: http`
  entry proactively, without connecting to anything, checked PER SOURCE FILE (the
  same 3 static locations + the dynamic `.reyn/config/mcp.yaml` the placement check
  above scans). Fix: change `type: http` to `type: streamable-http` by hand in the
  file the finding names.
- **Agent `profile.yaml` unknown keys** (#5455 ①) — every
  `.reyn/agents/<name>/profile.yaml`, checked against `AgentProfile`'s own field
  list (`reyn.runtime.profile.unknown_profile_keys`) — a different operator-editable
  surface from the three above, with its own closed vocabulary (dataclass fields,
  not `ReynConfig`'s schema), so it is its own check rather than an entry folded
  into the top-level unknown-key checks. Reported one line per agent directory
  that has an unrecognized key. A key that is real today but later removed from
  `AgentProfile` (e.g. #5095's `broker_identity`) leaves this line behind in an
  operator's file with no other signal — it is read, kept in no in-memory state,
  and does nothing. Fix: remove the key(s) from the file named.

`reyn config migrate` only rewrites an entry whose registered rename has an automatic
destination (a plain rename, no value transform); a rename that also transforms the
value is reported as "needs manual review" instead, never guessed at.

When every check above comes back clean, the "no issues" line names the population
it actually walked (#5455 ③ — the earlier text just said "no unknown ... config keys
found" without saying it had never looked at `profile.yaml` at all): the policy
tier, the hot-reload IN-set, and every `.reyn/agents/<name>/hooks.yaml` and
`profile.yaml`. A future eighth check added to this command must be added to that
same walked-population list too — nothing enforces that automatically.

## Examples

```bash
reyn config
reyn config fields
reyn config get safety.loop.max_router_iterations
reyn config set model strong
reyn config set safety.loop.max_router_iterations 50
reyn config validate
reyn config migrate --dry-run
reyn config migrate-mcp --dry-run
```

## See also

- [Reference: `reyn.yaml`](../config/reyn-yaml.md)
