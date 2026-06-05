---
type: how-to
topic: config
audience: [human]
applies_to: [reyn.yaml, skill.md]
---

# Configure the sandbox

reyn's sandboxed execution isolates commands run by a skill from the rest of
the host system. This page explains how to choose a backend, what each backend
enforces, and how to declare sandbox policy in a skill.

## Choose a backend

Set `sandbox.backend` in `reyn.yaml`:

```yaml
sandbox:
  backend: auto          # auto | seatbelt | landlock | noop  (default: auto)
  on_unsupported: warn   # warn | error | ignore               (default: warn)
```

`auto` (the default) picks the best available backend for the current platform:

| Platform | Condition | Backend selected |
|---|---|---|
| macOS | `sandbox-exec` present | Seatbelt |
| Linux | kernel ≥ 5.13, `landlock` package installed | Landlock |
| Other / fallback | — | Noop |

`on_unsupported` controls what happens when you force a backend that is
unavailable on the current platform:

| Value | Behaviour |
|---|---|
| `warn` (default) | Log a warning and fall back to Noop |
| `error` | Raise an error — useful in environments where containment is required |
| `ignore` | Silently fall back to Noop |

## Declare policy in a skill

A skill declares what the sandboxed process may do in its `skill.md`
frontmatter under `sandbox_policy:`:

```yaml
# skill.md
sandbox_policy:
  write_paths:
    - "{{workspace}}/output"
  network: false
  allow_subprocess: false
  timeout_seconds: 120
```

### Policy fields

| Field | Type | Default | Meaning |
|---|---|---|---|
| `network` | bool | `false` | Allow outbound network access. The primary exfiltration gate — off by default. |
| `write_paths` | list of paths | `[]` | Paths the process may write. Write implies read for these paths. This is the tight guard — only listed paths are writable. |
| `read_deny_paths` | list of paths | [see below](#read_deny_paths-default) | Sensitive paths to deny from the broad read surface (defense-in-depth). |
| `read_paths` | list of paths | `[]` | Legacy field — documents intended read targets. Under the current broad-read model, reads are not restricted to this list; the field is preserved for backward compatibility. |
| `allow_subprocess` | bool | `false` | Allow the process to spawn child processes. |
| `env_passthrough` | list of strings | `[]` | Environment variable names to pass through to the sandboxed process. `PATH` is always passed through. |
| `timeout_seconds` | int | `60` | Wall-clock timeout. The backend kills the process when this elapses. |

### Scoping model: broad-read, tight-write, network gate

reyn uses a **broad-read** scoping model:

- **Reads are broad by default.** The sandboxed process can read most of the
  filesystem. This avoids the system-path enumeration problem that breaks
  Landlock on Linux (where you must list every system library the process needs
  to load).
- **Network is the exfiltration gate.** With `network: false` (the default),
  the process can read broadly but cannot exfiltrate data. When you set
  `network: true`, you accept that the network surface is open.
- **Writes are tight.** Only paths in `write_paths` are writable.
- **`read_deny_paths` is defense-in-depth.** Even though reads are broad,
  a default deny-list covers OS-level credential locations so they cannot be
  read even if network is disabled. See below.

### `read_deny_paths` default {#read_deny_paths-default}

The default `read_deny_paths` denies access to common OS credential stores:

```
~/.ssh
~/.aws
~/.gnupg
~/.config/gcloud
~/.kube
~/.docker/config.json
~/.netrc
```

You can override this list to add or remove paths. Workspace-internal files
(e.g. a `.env` in the project root) are intentionally **not** in the default —
the agent operates inside the workspace, so a blanket workspace deny would
break legitimate reads.

**Note:** `read_deny_paths` is enforced only on backends that can express a
deny-after-allow rule. See [per-backend behavior](#per-backend-behavior) below.

## Per-backend behavior {#per-backend-behavior}

### Seatbelt (macOS)

Uses `sandbox-exec` with an SBPL deny-default profile. This is the strongest
containment mode available on macOS.

| Policy field | Enforcement |
|---|---|
| `write_paths` | Enforced — only listed paths (and their read access) are permitted |
| `network` | Enforced — blocked unless `true` |
| `read_deny_paths` | **Enforced** — SBPL deny-after-allow rules carve sensitive paths from the broad read surface |
| `allow_subprocess` | Advisory — `process-fork` is always permitted for binary bootstrap; this field is recorded in the audit log but does not prevent subprocess spawning at the SBPL level |
| `timeout_seconds` | Enforced — process is killed on expiry |

### Landlock (Linux)

Uses the Linux Landlock LSM with path-beneath rules. Landlock is an
allowlist-only mechanism — it cannot express a deny-after-allow rule.

| Policy field | Enforcement |
|---|---|
| `write_paths` | Enforced — path-beneath write rules |
| `network` | Enforced on Linux 6.7+ (ABI v4); a warning is logged on older kernels where network restriction is unavailable |
| `read_deny_paths` | **Not enforced** — Landlock cannot carve a subpath out of a broader allowed parent. The network gate (off by default) is the primary exfiltration control on Landlock. |
| `allow_subprocess` | Enforced via seccomp-BPF (when the `seccomp` dependency is available) |
| `timeout_seconds` | Enforced — process is killed on expiry |

### Noop

No containment is enforced. Policy fields are recorded in the audit log but
have no effect on what the process may do. Use this mode only in trusted
environments where platform sandbox mechanisms are unavailable.

### Container (in development)

Docker-based containment is in development. Container mount-mode is not yet
available.

## See also

- [Concepts: permission model](../../concepts/runtime/permission-model.md) — the broader permission and authorization model
- [How-to: manage permissions](manage-permissions.md) — declare and approve capability permissions in `skill.md`
