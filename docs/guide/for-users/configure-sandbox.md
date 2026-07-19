---
type: how-to
topic: config
audience: [human]
applies_to: [reyn.yaml, reyn run]
---

# Configure the sandbox

reyn's sandbox layer isolates subprocess execution at the operator level.
The operator sets the backend and policy in `reyn.yaml`; workflows do not control
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
| Linux | kernel ≥ 5.13, `sandbox-linux` package installed | Landlock **+ seccomp-BPF (both required)** |
| Other | — | Noop (audit-only, no enforcement) |

A backend from this table is used only if it **passes an enforcement self-test** on your machine — see [Reyn checks that your sandbox really sandboxes](#reyn-checks-that-your-sandbox-really-sandboxes) below.

`on_unsupported` controls what happens when no usable backend is available — either because the one you forced is not present on this platform, or because it **is present but does not actually enforce**:

| Value | Behaviour |
|---|---|
| `warn` (default) | Log a warning and fall back to Noop |
| `error` | Raise an error — use this when enforcement is a hard requirement |
| `ignore` | Silently fall back to Noop |

## Reyn checks that your sandbox really sandboxes

When Reyn picks a backend, it first proves the backend works **on your machine**. It launches short subprocesses through that backend and tries two things the policy forbids: writing a file outside the writable paths, and spawning a process while `allow_subprocess` is off. Both must be refused. If either goes through, the backend is not enforcing what it claims, and Reyn treats it exactly as if it were not installed — applying your `on_unsupported` setting.

This matters because "the sandbox is installed" and "the sandbox works" are different things. A backend can be present and importable while enforcing nothing at all — right OS, package imports fine, and yet every restriction silently absent. Checking only for presence cannot tell those apart. So Reyn checks the thing you actually care about: whether a forbidden action gets refused.

The two **checks** are separate on purpose, because they can fail independently — different mechanisms enforce them, and on Linux one can be dead while the other works. But the **protection** does not decompose the same way, which is why Reyn requires both rather than keeping whichever one passes.

On Linux, path rules come from Landlock and the syscall gate from seccomp-BPF. Without the syscall gate, Landlock's write boundary is real but not airtight: it governs ordinary writes, and Landlock has no `chmod` right at all, so with seccomp absent a sandboxed process can still `truncate` a file or `chmod` a directory **outside** `write_paths` — no layer stops it. Measured on Linux 6.8 with Landlock enforcing and the syscall filter absent: `open()` on a file outside `write_paths` was refused, while `os.truncate()` on that same file **succeeded and emptied it**. The syscall filter is what refuses those calls, by not listing them.

So "writes are enforced, spawning is not" is not a coherent state to ship, and a write-only check would have called that host sandboxed.

What you should expect to see:

- **Normally, nothing.** A working sandbox passes silently. The check costs tens of milliseconds, once, and only when a run actually uses the sandbox.
- **If your sandbox is not enforcing**, a warning at startup naming what was attempted and what happened — instead of silently unsandboxed runs.
- **With `on_unsupported: error`**, Reyn refuses to run rather than execute AI-generated code unsandboxed. This setting now works against a broken sandbox, not just a missing one.

If you see the warning, your AI code has been running without isolation. The message names the backend and the failure so you can fix it or fail closed deliberately.

**Scope.** The check verifies the filesystem write boundary and the process-spawn gate. It does not exercise the network gate or `read_deny_paths`, so a passing check means two restrictions were proven — a good signal, not a guarantee of every restriction listed below. The spawn check doubles as evidence that the Linux syscall filter **loaded at all**, which is what keeps the `truncate`/`chmod` hole above closed.

## Set the agent-level sandbox policy

`sandbox.policy` lets the operator declare a deterministic, operator-controlled
sandbox policy. When set, it applies to all `sandboxed_exec` ops **and** to the
`SandboxLayer` of the permission intersection — a workflow or the LLM cannot widen it.

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
| `write_paths` | list of paths | `[]` | Paths the process may write (tight guard). Write implies read — a path listed here is also re-opened for *reading* even if `read_deny_paths` would deny it, so grant specific directories rather than `~`. `~` is expanded. |
| `read_deny_paths` | list of paths | `[]` | Sensitive paths to deny from the broad read surface (defense-in-depth). Enforced only on backends that support deny-after-allow (Seatbelt); not enforceable on Landlock. |
| `read_paths` | list of paths | `[]` | Legacy — formerly the strict read allowlist. Reads are broad by default; this field now documents intended read targets only. |
| `allow_subprocess` | bool | `false` | Allow child-process spawning. Enforced on Linux (seccomp) and macOS (Seatbelt). |
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
| `network` | Enforced. A loopback-only `network-bind` (`localhost:*`) is always allowed regardless of `network`, mirroring Landlock's `socket`/`bind` exception above ([#3060](https://github.com/tya5/reyn/issues/3060)) — `network-outbound`/`network-inbound` stay gated on `network`. |
| `read_deny_paths` | **Enforced** — SBPL deny-after-allow |
| `allow_subprocess` | **Enforced** — denies `process-fork` when off; the target's own exec still works via `process-exec*` |
| `timeout_seconds` | Enforced |

### Landlock (Linux)

Uses the Linux Landlock LSM with path-beneath allowlist rules.

| Field | Enforcement |
|---|---|
| `write_paths` | Enforced — path-beneath write rules |
| `network` | **Enforced, unconditionally** ([#3030](https://github.com/tya5/reyn/issues/3030) fixed). Landlock itself never restricts network on any kernel: the pinned `landlock` package exposes no network-rule API, so the deny is carried entirely by a seccomp-BPF default-deny **allowlist** — every syscall not named (including `connect`/`sendto`/`sendmsg`/`accept`/`listen` when `network: false`, and unconditionally io_uring's `io_uring_setup`/`io_uring_enter`, which a syscall-name denylist cannot express) is refused. This filter used to be skipped ENTIRELY whenever `allow_subprocess: true` — the stdio MCP default — which silently dropped the network gate along with it; it now loads unconditionally, so `network: false` is enforced regardless of `allow_subprocess`. `socket`/`bind` are the two exceptions, always allowed regardless of `network` ([#3060](https://github.com/tya5/reyn/issues/3060)): neither one alone transmits or receives a byte, and a benign import-time IPv6-support probe in a common HTTP-client dependency (`bind`s to `::1` on port 0 and never `connect`s) used to be refused as collateral damage. Dialing an actual peer still requires `connect`, which stays gated. |
| `read_deny_paths` | **Not enforced** — Landlock is allowlist-only and cannot carve a subpath out of an allowed parent. The network gate (see the `network` row) is the compensating exfiltration control, and — since #3030 — applies regardless of `allow_subprocess`. Do not rely on this platform to contain a process that can read a secret; network denial only stops it leaving. |
| `allow_subprocess` | **Enforced** — seccomp-BPF refuses `fork`/`clone`. Landlock is not selected unless the self-test witnesses this deny on your host, so this is a checked claim rather than a hope that `pyseccomp` is installed and loading |
| `timeout_seconds` | Enforced |

### Noop

No containment enforced. Policy fields are recorded in the audit log but have no
effect. Use only in trusted environments where enforcement is unavailable.

## Run in a container (mount mode)

For the strongest isolation — or to run workflows against a consistent Linux
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

### devcontainer.json

If the workspace ships a `devcontainer.json` (`.devcontainer/devcontainer.json`
or `.devcontainer.json`), reyn reads a minimal subset to seed the launch:
`image`, `postCreateCommand`, `mounts`, and `remoteUser`. An explicit `--image`
always overrides the devcontainer.

- **Image-based** (`image: ...`) — launched directly.
- **Build-based** (`dockerFile` / `build`) — reyn **builds the Dockerfile on
  demand** (`docker build`) and launches the result. The built image is tagged
  by content hash, so it is rebuilt only when the Dockerfile / build args /
  target change. `build.args` and `build.context` are honored.
- **Compose-based** (`dockerComposeFile`) — not supported (the launcher is
  single-container); reyn warns and falls back to the default image.

!!! warning "Build runs the workspace Dockerfile on your host"
    Building a build-based devcontainer runs that Dockerfile's `RUN` steps on
    your host Docker daemon at **build time** — these are **not** confined by
    reyn's runtime sandbox (the network-off / non-root / read-only-rootfs flags
    apply to the *running* container, not to `docker build`). This is the same
    trust model as VS Code's "Reopen in Container": only use build-based
    devcontainers from workspaces you trust. reyn logs the build for visibility;
    `--env-backend=docker` is the opt-in.

## See also

- [Concepts: Sandbox and permissions](../../concepts/architecture/sandbox-vs-permission.md) — why sandbox and permissions are orthogonal
- [Concepts: Sandbox](../../concepts/runtime/sandbox.md) — backend field reference and scoping model details
- [Reference: `reyn.yaml`](../../reference/config/reyn-yaml.md) — full `sandbox:` config schema
- [How-to: Manage permissions](manage-permissions.md) — declare and approve workflow-level capability permissions
