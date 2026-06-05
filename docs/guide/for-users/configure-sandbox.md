---
type: how-to
topic: config
audience: [human]
applies_to: [reyn.yaml, reyn run]
---

# Configure the sandbox

reyn's sandbox layer isolates subprocess execution at the operator level.
The operator sets the backend and policy in `reyn.yaml`; skills do not control
their own containment. Sandbox is orthogonal to permissions — see
[Sandbox and permissions](../../concepts/architecture/sandbox-vs-permission.md).

## Choose a backend

```yaml
# reyn.yaml
sandbox:
  backend: auto          # auto | seatbelt | landlock | noop
  on_unsupported: warn   # warn | error | ignore
```

`backend: auto` (the default) picks the best available backend for the current
platform:

| Platform | Condition | Backend |
|---|---|---|
| macOS | `sandbox-exec` available | Seatbelt (SBPL deny-default) |
| Linux | kernel ≥ 5.13, `sandbox-linux` package installed | Landlock (+ optional seccomp-BPF) |
| Other | — | Noop (audit-only, no enforcement) |

`on_unsupported` controls what happens when you force a backend that is unavailable:

| Value | Behaviour |
|---|---|
| `warn` (default) | Log a warning and fall back to Noop |
| `error` | Raise an error — use this when enforcement is a hard requirement |
| `ignore` | Silently fall back to Noop |

## Set the agent-level sandbox policy

`sandbox.policy` lets the operator declare a deterministic, operator-controlled
sandbox policy. When set, it applies to all `sandboxed_exec` ops **and** to the
`SandboxLayer` of the permission intersection — a skill or the LLM cannot widen it.

```yaml
sandbox:
  backend: auto
  policy:
    network: false
    write_paths:
      - "{{workspace}}/output"
    read_deny_paths:
      - "~/.ssh"
      - "~/.aws"
    timeout_seconds: 120
```

When `sandbox.policy` is absent (the default), there is no agent-level
restriction: op-level fields govern, and the SandboxLayer is unrestricted.

### Policy fields

| Field | Type | Default | Meaning |
|---|---|---|---|
| `network` | bool | `false` | Allow outbound network. The primary exfiltration gate. |
| `write_paths` | list of paths | `[]` | Paths the process may write (tight guard). Write implies read. |
| `read_deny_paths` | list of paths | `[]` | Sensitive paths to deny from the broad read surface (defense-in-depth). Enforced only on backends that support deny-after-allow (Seatbelt); not enforceable on Landlock. |
| `read_paths` | list of paths | `[]` | Legacy — formerly the strict read allowlist. Reads are broad by default; this field now documents intended read targets only. |
| `allow_subprocess` | bool | `false` | Allow child-process spawning. Advisory under Seatbelt. |
| `env_passthrough` | list of strings | `[]` | Env vars passed through to the process. `PATH` is always passed. |
| `timeout_seconds` | int | `60` | Wall-clock limit; process is killed on expiry. |

### Scoping model

reyn uses a **broad-read, tight-write, network-gated** model:

- **Reads are broad.** The process can read most of the filesystem. System-path
  enumeration for dylib loading works without enumeration in policy.
- **Network is the exfiltration gate.** With `network: false` (the default),
  the process can read broadly but cannot send data out.
- **Writes are tight.** Only paths in `write_paths` are writable.
- **`read_deny_paths` is defense-in-depth.** Carves out sensitive locations from
  the broad read surface where the backend can express a deny-after-allow rule.

## Per-backend behavior

### Seatbelt (macOS)

Uses `sandbox-exec` with an SBPL deny-default profile. Strongest containment
on macOS.

| Field | Enforcement |
|---|---|
| `write_paths` | Enforced |
| `network` | Enforced |
| `read_deny_paths` | **Enforced** — SBPL deny-after-allow |
| `allow_subprocess` | Advisory — process-fork always permitted for binary bootstrap |
| `timeout_seconds` | Enforced |

### Landlock (Linux)

Uses the Linux Landlock LSM with path-beneath allowlist rules.

| Field | Enforcement |
|---|---|
| `write_paths` | Enforced — path-beneath write rules |
| `network` | Enforced on Linux 6.7+ (ABI v4); warning logged on older kernels |
| `read_deny_paths` | **Not enforced** — Landlock is allowlist-only and cannot carve a subpath out of an allowed parent. The network gate is the primary exfiltration control. |
| `allow_subprocess` | Enforced via seccomp-BPF when available |
| `timeout_seconds` | Enforced |

### Noop

No containment enforced. Policy fields are recorded in the audit log but have no
effect. Use only in trusted environments where enforcement is unavailable.

## Run in a container (mount mode)

For the strongest isolation — or to run skills against a consistent Linux
environment regardless of the host OS — use the Docker backend:

```bash
# Launch a new container (mount mode)
reyn run my_skill --env-backend=docker

# Use a specific image
reyn run my_skill --env-backend=docker --image my-registry/my-image:latest

# Add extra bind mounts
reyn run my_skill --env-backend=docker \
  --mount /data/inputs:/data/inputs:ro \
  --mount /data/outputs:/data/outputs:rw

# Keep the container after the run (for inspection)
reyn run my_skill --env-backend=docker --keep-container

# Attach to an already-running container
reyn run my_skill --env-backend=docker --container my-container --repo-dir /workspace
```

In mount mode, the workspace root is automatically bind-mounted at `/workspace`
inside the container. The sandbox backend used inside the container is determined
by `reyn.yaml sandbox.backend` as usual (typically `landlock` on Linux).

### Default image

When `--image` is omitted, reyn uses a bundled base image built for the current
platform. To use a custom image, pass `--image` or set the default in `reyn.yaml`
(see [`reyn.yaml` reference](../../reference/config/reyn-yaml.md)).

## See also

- [Concepts: Sandbox and permissions](../../concepts/architecture/sandbox-vs-permission.md) — why sandbox and permissions are orthogonal
- [Concepts: Sandbox](../../concepts/runtime/sandbox.md) — backend field reference and scoping model details
- [Reference: `reyn.yaml`](../../reference/config/reyn-yaml.md) — full `sandbox:` config schema
- [How-to: Manage permissions](manage-permissions.md) — declare and approve skill-level capability permissions
