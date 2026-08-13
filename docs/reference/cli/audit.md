---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn audit]
---

# `reyn audit`

A **static**, install-time / on-demand safety scan of installed agents, MCP plugin
configs, secrets, and delegation topology (#1864, #1822 Part 3). It READS config —
`reyn.yaml`'s `mcp:`/`delegation:` sections, `~/.reyn/secrets.env`'s file mode,
`.reyn/topologies/*.yaml`, `.reyn/capability_profiles/*.yaml` — and pattern-scans them.
It never imports or executes code, unlike the separate #1822 S1-S5 **runtime**
content-threat scan. The OpenClaw `audit-*` analog.

## Synopsis

```
reyn audit [--json]
```

**Exit code**: non-zero (1) only on a **HIGH**-severity finding — CI-usable as a gate.
MED/INFO findings never fail the exit code.

## Rules

### 1. Secrets permission (HIGH)

`~/.reyn/secrets.env` must be `chmod 600` (the convention `security/secrets/store.py`
writes it with). Any group/other-accessible mode is flagged HIGH.

### 2. Gateway exposure — MCP plugin configs

Reads `reyn.yaml`'s `mcp:` servers directly and flags, per server:

- a `command` field (subprocess spawn) — **HIGH**
- a secret-looking `env` key (matches `KEY`/`TOKEN`/`SECRET`/`PASSWORD`/`CREDENTIAL`/
  `PASSWD`) — **HIGH**
- a `url` field (network egress) — **INFO**
- every configured server, regardless of the above — **INFO** (enumeration)

### 3. Delegation-unsafe capability (#2081 S3)

Flags a **delegate-REACHABLE** topology role (a member with an inbound `can_send`
edge — the A2A request path is `can_send`-gated, so this is reachability-precise: an
outbound-only role like a hierarchy's top coordinator legitimately holding
`delegate_to_agent` is NOT flagged, avoiding a false HIGH that would wrongly block a
deploy) whose bound `capability_profile`, or the `_delegate.yaml` override, permits a
dangerous class:

- re-delegation / exec — **HIGH**
- memory-write / destructive-FS — **MED**

Plus an **INFO** posture nudge when `delegation.capability_default=inherit` while any
topology has a delegation edge — delegated agents inherit the spawner's full
capability by default; the finding names `delegation.capability_default=deny` as the
restrictive alternative.

The `_delegate.yaml` override (the global delegate floor) is scanned unconditionally,
since a re-grant there applies to every unbound delegate regardless of topology
reachability.

Resolved against the actual project root (`_find_project_root`, not `Path.cwd()`) —
running `reyn audit` from a subdirectory scans the real project's
`.reyn/topologies`/`.reyn/capability_profiles`, not a phantom (usually absent) copy
under the subdirectory (#4204).

## Output

```bash
$ reyn audit

  [HIGH] gateway:subprocess         mcp:my-server: spawns a subprocess: command='node server.js'
  [INFO] gateway:mcp-server         mcp:my-server: MCP server configured

reyn audit: 2 finding(s) — 1 HIGH, 0 MED, 1 INFO
```

A clean project reports `reyn audit: no findings.`

```bash
$ reyn audit --json
[
  {
    "location": "mcp:my-server",
    "rule": "gateway:subprocess",
    "severity": "HIGH",
    "detail": "spawns a subprocess: command='node server.js'"
  }
]
```

## Related

- [`reyn mcp`](mcp.md) — manage the MCP server configs `reyn audit`'s gateway rule reads
- [Concepts: permission model](../../concepts/runtime/permission-model.md) — the
  capability-profile / delegation model rule 3 checks against

`reyn plugin` (install/uninstall the plugins this command audits) has no reference
page of its own yet — part of the same #4489 gap this page closes one piece of; link
here once it lands.
