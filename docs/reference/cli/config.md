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
never gates (owner ruling: warn, never hard-fail, anywhere). It checks four things,
each printed as its own labeled section (never merged into one list — the fix differs
per section, and merging would lose "which one do I fix, and how"). Note the last
check (hook entries) covers three separate config *files* under one section, not one:

- **Unrecognized/renamed/removed keys, policy tier** (`reyn.yaml` / `reyn.local.yaml` /
  `~/.reyn/config.yaml`) — the same check `load_config`'s own startup warning runs.
  Fix: edit the file and restart, or run `reyn config migrate` for a renamed key with
  an automatic destination. A REMOVED key (#4375 — no successor exists, unlike a
  rename) is reported in its own "REMOVED ... delete them" section — `migrate` never
  auto-rewrites it (there is no destination) and never auto-deletes it either; the
  operator's next action is "delete this line", not "rewrite it".
- **Known keys currently disabled by another key's value** — the key is real and
  correctly spelled, but the current configuration makes it a no-op (e.g.
  `action_retrieval.universal_wrappers_enabled` has no effect while `tool_use.scheme`
  resolves to `enumerate-all`). Each entry names the key it depends on and the fix.
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

`reyn config migrate` only rewrites an entry whose registered rename has an automatic
destination (a plain rename, no value transform); a rename that also transforms the
value is reported as "needs manual review" instead, never guessed at.

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
