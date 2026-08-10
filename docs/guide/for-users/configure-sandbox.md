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

When Reyn picks a backend, it first proves the backend works **on your machine**. It launches short subprocesses through that backend and tries two things the policy forbids: writing a file outside the writable paths, and spawning a process with `subprocess: false` set. Both must be refused. If either goes through, the backend is not enforcing what it claims, and Reyn treats it exactly as if it were not installed — applying your `on_unsupported` setting.

This matters because "the sandbox is installed" and "the sandbox works" are different things. A backend can be present and importable while enforcing nothing at all — right OS, package imports fine, and yet every restriction silently absent. Checking only for presence cannot tell those apart. So Reyn checks the thing you actually care about: whether a forbidden action gets refused.

The two **checks** are separate on purpose, because they can fail independently — different mechanisms enforce them, and on Linux one can be dead while the other works. But the **protection** does not decompose the same way, which is why Reyn requires both rather than keeping whichever one passes.

On Linux, path rules come from Landlock and the syscall gate from seccomp-BPF. Without the syscall gate, Landlock's write boundary is real but not airtight: it governs ordinary writes, and Landlock has no `chmod` right at all, so with seccomp absent a sandboxed process can still `truncate` a file or `chmod` a directory **outside** `allow_write_paths` — no layer stops it. Measured on Linux 6.8 with Landlock enforcing and the syscall filter absent: `open()` on a file outside `allow_write_paths` was refused, while `os.truncate()` on that same file **succeeded and emptied it**. The syscall filter is what refuses those calls, by not listing them.

So "writes are enforced, spawning is not" is not a coherent state to ship, and a write-only check would have called that host sandboxed.

What you should expect to see:

- **Normally, nothing.** A working sandbox passes silently. The check costs tens of milliseconds, once, and only when a run actually uses the sandbox.
- **If your sandbox is not enforcing**, a warning at startup naming what was attempted and what happened — instead of silently unsandboxed runs.
- **With `on_unsupported: error`**, Reyn refuses to run rather than execute AI-generated code unsandboxed. This setting now works against a broken sandbox, not just a missing one.

If you see the warning, your AI code has been running without isolation. The message names the backend and the failure so you can fix it or fail closed deliberately.

**Scope.** The check verifies the filesystem write boundary and the process-spawn gate. It does not exercise the network gate or `deny_read_paths`, so a passing check means two restrictions were proven — a good signal, not a guarantee of every restriction listed below. The spawn check doubles as evidence that the Linux syscall filter **loaded at all**, which is what keeps the `truncate`/`chmod` hole above closed.

## Set the agent-level sandbox policy

`sandbox.policy` lets the operator declare a deterministic, operator-controlled
sandbox policy, in a config vocabulary decoupled from the internal enforcement
fields it maps to (`#3823`). When set, it applies to all `sandboxed_exec` ops
**and** to the `SandboxLayer` of the permission intersection for the
`network`/`subprocess`/`env` axes — a workflow or the LLM cannot widen it.
`allow_write_paths` (and the read/write deny-lists) do NOT participate in that
intersection: they are values an operator cannot know in advance (the op
declares what directory it needs), so the kernel backend consumes them
directly rather than through the permission ∩ (`#3901`).

```yaml
sandbox:
  backend: auto
  mode: compat           # compat | strict — the DEFAULT for any key left unset below
  policy:
    network: false
    allow_write_paths:
      - "{{workspace}}/output"
    deny_read_paths:
      - "~/.ssh"
      - "~/.aws"
    timeout_seconds: 120
```

When `sandbox.policy` is absent (the default), there is no agent-level
restriction: op-level fields govern, and the SandboxLayer is unrestricted.
Unknown keys under `policy` fail loudly at config load — a typo never
silently resolves to "nothing to deny".

### Policy fields

Every field except `allow_write_paths` defaults to full compat under
`mode: compat` (the default; owner ruling, `#3901`/`#3823`): the sandbox's job
is bounding what happens *behind* a permitted action, not re-deciding what the
launching shell could already do. Setting `mode: strict` flips every axis
except `allow_write_paths` (and read, which has no mode-based default at all)
to its closed default — an explicit key under `policy` always wins over `mode`.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `network` | bool | `true` (compat); `false` under `mode: strict` | Allow outbound network. The primary exfiltration gate — a config-allowed host is still denied under `network: false`. Set `network: false` explicitly to close it. |
| `subprocess` | bool | `true` (compat) — positive framing, `true` = allowed; `false` under `mode: strict` | Whether the process may spawn children. Set `subprocess: false` to deny it. |
| `allow_write_paths` | list of paths | `[]` | Paths the process may write (tight guard) — the one field that stays closed by default regardless of `mode` (an operator-unknowable value, `#3901`). Write implies read — a path listed here is also re-opened for *reading* even if `deny_read_paths` would deny it, so grant specific directories rather than `~`. `~` is expanded. |
| `deny_read_paths` | list of paths | `[]` (compat) | Sensitive paths to deny from the broad read surface (defense-in-depth, **opt-in**). Enforced only on backends that support deny-after-allow (Seatbelt); not enforceable on Landlock. Before `#3901` this defaulted to 7 OS-level credential paths — set it explicitly to get that protection back. No mode-based default (no `allow_read_paths` concept, #1199) — `strict` cannot narrow reads any tighter than `compat`. Denies only the READ axis; see `deny_write_paths` for the write axis. |
| `deny_write_paths` | list of paths | `[]` | The write axis's own deny-list (`#3901`), mirroring `deny_read_paths`. Denies only the WRITE axis — before `#3901` a `deny_read_paths` entry also (undocumentedly) denied writes on Seatbelt; that coupling is gone, so list a path in both fields if you want it protected on both axes. |
| `allow_env_names` | list of strings \| `null` | `null` (deny-list-only); `[]` under `mode: strict` | SWITCHES the env axis to allow-list semantics when set to a list — only those names pass through (still intersected with `deny_env_names`, deny always wins). |
| `deny_env_names` | list of strings | `[]` (compat) | Env vars WITHHELD from the process. Empty (the default) means the whole environment passes through, same trust level as the launching shell. |
| `timeout_seconds` | int | `60` | Wall-clock limit; process is killed on expiry. |

**`subprocess: false` is the cheapest, most predictable hardening available for a workload that never needs to spawn anything.** It is a single boolean, and its effect is total and immediate: child-process spawning is denied outright, with no partial states to reason about later. If your workload genuinely needs to exec (a build step, a CLI wrapper), this setting isn't for you — you're bound instead by the sandbox boundary plus the audit trail every exec leaves (`sandboxed_exec_started`/`_completed`/`_cancelled` record the `argv` — see [Reference: events](../../reference/runtime/events.md)).

### Scoping model

reyn uses a **broad-read, tight-write, network-open-by-default** model:

- **Reads are broad.** The process can read most of the filesystem. System-path
  enumeration for dylib loading works without enumeration in policy.
- **Network is open by default.** `network` defaults to `true` (owner decision,
  2026-06-05; reaffirmed as full compat across every non-`allow_write_paths`
  axis by `#3901` owner ruling B) — a sandboxed process can reach the network
  unless you set `network: false` explicitly. This follows reyn's standing
  UX-over-security posture: security mechanisms here are opt-in, not opt-out —
  see [Protect credentials from sandboxed
  commands](protect-credentials-in-shell-commands.md) for what this means for
  a command that can read a secret.
- **Writes are tight.** Only paths in `allow_write_paths` are writable — the
  one axis that stays closed by default regardless of `mode` (an
  operator-unknowable value, `#3901`).
- **`deny_read_paths`/`deny_write_paths` are defense-in-depth, opt-in.** Carve out
  sensitive locations from the broad read/write surface where the backend can
  express a deny-after-allow rule; empty (nothing carved out) by default.
- **`mode: strict`** flips network/subprocess/env to their closed defaults in
  one setting, for an operator who wants the pre-compat posture back without
  writing every key explicitly — see [reyn.yaml §
  `sandbox.mode`](../../reference/config/reyn-yaml.md#sandbox-block).

## Per-backend behavior

### Seatbelt (macOS)

Uses `sandbox-exec` with an SBPL deny-default profile. Strongest containment
on macOS.

| Field | Enforcement |
|---|---|
| `allow_write_paths` | Enforced |
| `network` | Enforced. A loopback-only `network-bind` (`localhost:*`) is always allowed regardless of `network`, mirroring Landlock's `socket`/`bind` exception above ([#3060](https://github.com/tya5/reyn/issues/3060)) — `network-outbound`/`network-inbound` stay gated on `network`. |
| `deny_read_paths` | **Enforced** — SBPL deny-after-allow |
| `deny_write_paths` | **Enforced** — SBPL deny-after-allow, independent of `deny_read_paths` (#3901: each denies only its own axis) |
| `subprocess` | **Enforced** — `subprocess: false` denies `process-fork`; the target's own exec still works via `process-exec*` |
| `timeout_seconds` | Enforced |

### Landlock (Linux)

Uses the Linux Landlock LSM with path-beneath allowlist rules.

| Field | Enforcement |
|---|---|
| `allow_write_paths` | Enforced — path-beneath write rules |
| `network` | **Enforced, unconditionally** ([#3030](https://github.com/tya5/reyn/issues/3030) fixed). Landlock itself never restricts network on any kernel: the pinned `landlock` package exposes no network-rule API, so the deny is carried entirely by a seccomp-BPF default-deny **allowlist** — every syscall not named (including `connect`/`sendmsg`/`accept`/`listen` when `network: false`, and unconditionally io_uring's `io_uring_setup`/`io_uring_enter`, which a syscall-name denylist cannot express) is refused. This filter used to be skipped ENTIRELY whenever `subprocess` was `true` (allowed) — the stdio MCP default — which silently dropped the network gate along with it; it now loads unconditionally, so `network: false` is enforced regardless of `subprocess`. Two exceptions, always allowed regardless of `network` ([#3060](https://github.com/tya5/reyn/issues/3060)): **(1)** `socket`/`bind` — neither one alone transmits or receives a byte, and a benign import-time IPv6-support probe in a common HTTP-client dependency (`bind`s to `::1` on port 0 and never `connect`s) used to be refused as collateral damage; **(2)** `sendto`/`recvfrom` **when their address argument is NULL** — the connected AF_UNIX socketpair CPython's asyncio event loop uses to wake itself (`send`/`recv` lower to `sendto`/`recvfrom` with a NULL address), whose wholesale denial left every stdio MCP server's loop unable to pump, so the server served 0 bytes. Dialing an actual peer still requires `connect` (denied), and the **addressed** form of `sendto` (`sendto(fd, …, &sockaddr, …)` — real UDP egress) has a non-NULL address and stays denied by that same condition. |
| `deny_read_paths` | **Not enforced** — Landlock is allowlist-only and cannot carve a subpath out of an allowed parent. The network gate (see the `network` row) is the compensating exfiltration control, and — since #3030 — applies regardless of `subprocess`. Do not rely on this platform to contain a process that can read a secret; network denial only stops it leaving. |
| `deny_write_paths` | **Not enforced** — same allowlist-only limitation as `deny_read_paths` above. |
| `subprocess` | **Enforced** — seccomp-BPF refuses `fork`/`clone` when `subprocess: false`. Landlock is not selected unless the self-test witnesses this deny on your host, so this is a checked claim rather than a hope that `pyseccomp` is installed and loading |
| `timeout_seconds` | Enforced |

### Noop

No containment enforced. Policy fields are recorded in the audit log but have no
effect. Use only in trusted environments where enforcement is unavailable.

### When Reyn warns that an axis is not enforced — and when it stays quiet

If you configure `deny_read_paths` or `deny_write_paths` and the selected backend
cannot express them, Reyn says so at dispatch time: a `sandbox_axis_unenforced`
audit event plus a `WARNING` log line naming the axes, the backend, and the
reason ("Landlock cannot express a deny-list — LSM allowlist-only constraint").
The policy is still written to the audit log; it simply was not applied for
those axes.

**Scope — this warning covers one gap, not every gap.** It fires only for the
two deny-list fields, and only on a backend that is specifically deny-list
incapable, which today means Landlock alone. It is not a general "your policy
was not enforced" check.

🔴 **Silence is therefore not a clean bill of health.** The check asks "can
this backend express a deny-list?", not "does this backend enforce what you
configured?" — so a backend that enforces little or nothing passes it in
silence.

The sharpest case is the **Docker backend** (`--env-backend=docker`, below).
It is a sandbox backend — the same object serves as both the environment and
the sandbox backend for a container agent — and its `run()` **honors only
`policy.timeout_seconds`**. Configure `allow_write_paths`, `network` or
`subprocess` under it and none of them is applied, and **no
`sandbox_axis_unenforced` warning is emitted**, because Docker is not
deny-list-incapable in the sense this check tests for; it is simply not
enforcing those axes at all, which the check does not look at. Isolation does
come from the container boundary itself — but it is the image and the mount
set that provide it, not the policy fields you wrote.

Noop is the same shape with nothing left to fall back on: it enforces nothing,
records the policy for audit, and likewise warns about nothing.

A quiet run under Docker or Noop and a quiet run under Seatbelt are
indistinguishable from this signal alone.

**What to rely on instead:** the per-backend tables above state, field by field,
what each backend actually enforces. Read the table for the backend you are
running; treat the warning as a targeted extra notice, not as the answer to
"was my policy applied?".

Two other mechanisms are easy to mistake for this one, and neither widens it:

- The startup self-test (see [Reyn checks that your sandbox really
  sandboxes](#reyn-checks-that-your-sandbox-really-sandboxes)) proves the write
  boundary and the process-spawn gate on your host. It runs at backend
  *selection*; this warning runs at op *dispatch*, once the policy's individual
  axes are known.
- Container (mount) mode below is a **different kind of isolation** from the
  three profile-based backends tabled above (Seatbelt, Landlock, Noop) — the
  container boundary, not a policy applied to a host process. It is still a
  sandbox backend as far as this warning is concerned, which is exactly why the
  silence described above covers it too.

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
