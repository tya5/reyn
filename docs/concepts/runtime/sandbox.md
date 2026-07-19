---
type: concept
topic: security
audience: [human, agent]
---

# Sandbox

Reyn's sandbox layer provides **operator-level containment** for subprocess execution. The operator configures the backend and scoping model via `reyn.yaml`; the OS enforces it without any OS code knowing which workflow is running (P3 / P7). Sandbox is orthogonal to permissions — see [Sandbox and permissions: orthogonal concerns](../architecture/sandbox-vs-permission.md).

The sandbox complements the [permission model](../runtime/permission-model.md): permissions enforce declared scope at dispatch time (before the op runs); the sandbox enforces the same boundaries at the system-call level while the subprocess is running. The two layers are independent and additive.

## `SandboxPolicy` field reference

Defined in `src/reyn/security/sandbox/policy.py`. Passed as fields on a `sandboxed_exec` Control IR op.

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `network` | `bool` | `false` | Allow outbound network connections. The primary exfiltration gate. |
| `write_paths` | `list[str]` | `[]` | Filesystem paths the subprocess may write (tight guard). Write implies read. `~` is expanded. |
| `read_deny_paths` | `list[str]` | [OS credential paths] | Sensitive paths denied from the broad read surface (defense-in-depth). Enforced only on backends that support deny-after-allow (Seatbelt). Default: `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gcloud`, `~/.kube`, `~/.docker/config.json`, `~/.netrc`. |
| `read_paths` | `list[str]` | `[]` | **Legacy** — formerly the read allowlist. Under the current broad-read model reads are not restricted to this list; retained for backward compatibility and as documentation of intended read targets. |
| `allow_subprocess` | `bool` | `false` | Allow the sandboxed process to spawn child processes. Enforced on Linux (seccomp) and macOS (Seatbelt: `process-fork` denied when off; the target's own exec still works via `process-exec*`). |
| `env_passthrough` | `list[str]` | `[]` | Host env-var NAMES to forward to the subprocess (a name absent from the host env forwards nothing). All others are stripped; `PATH` is always passed. |
| `env_explicit` | `dict[str, str]` | `{}` | Operator-declared key→value env pairs INJECTED into the subprocess independent of the host env (an MCP server's `.mcp.json` `env` block is the canonical source). Unlike `env_passthrough` (name-only), it carries the value, so a var present only in the server declaration is forwarded rather than dropped. Explicit value wins over a same-named passthrough. |
| `timeout_seconds` | `int` | `60` | Wall-clock limit; process is killed on expiry. |

## Backend selection table

`get_default_backend(config)` selects a backend at runtime based on platform, installed extras, and — before handing the backend to any caller — **whether that backend actually enforces on this host**. The `sandbox.backend` config key in `reyn.yaml` overrides automatic selection.

| Platform | Condition | Backend | Notes |
|----------|-----------|---------|-------|
| macOS | `sandbox-exec` present **and self-test passes** | `SeatbeltBackend` | SBPL deny-default profile via `sandbox-exec`. `sandbox-exec` is deprecated upstream but remains functional on macOS 26.3. Falls back to `NoopBackend` if the binary is absent. |
| Linux | kernel ≥ 5.13, `sandbox-linux` extra installed, **self-test passes** | `LandlockBackend` + seccomp-BPF | `pip install reyn[sandbox-linux]` required. Landlock does **not** restrict outbound network at any ABI — the pinned `landlock` package exposes no network-rule API, so a `network: false` policy is delivered by a different mechanism and the backend WARNs once to say so. |
| Linux | kernel < 5.13 or `sandbox-linux` not installed | `NoopBackend` | Audit-only; no enforcement. |
| Any | a backend is present but **fails its self-test** | `NoopBackend` (per `on_unsupported`) | The mechanism is installed but does not enforce. Treated exactly like an absent one. |
| Other | any | `NoopBackend` | Audit-only; no enforcement. |

When `NoopBackend` is used, Reyn logs a one-line `WARN` on first invocation. Set `sandbox.on_unsupported: error` to hard-fail instead.

### The enforcement self-test

A backend is selected only if it **fired a real deny on this machine**, on every axis it claims. At resolution Reyn launches short subprocesses through the backend's own wrap and attempts two things every real backend must refuse: a write to a path outside `write_paths`, and a process spawn under `allow_subprocess: false`. If either succeeds, the backend does not enforce what it advertises, and `sandbox.on_unsupported` applies as though the backend were absent.

This exists because "the mechanism is installed" and "the mechanism works" are different claims, and only the first was ever checked. A backend can be present, importable, and completely inert — so a check that asks only whether it is present will pass while nothing is enforced. The self-test asks the second question, on the host that makes the claim.

Two properties follow, and both matter more than the check itself:

- **It is verified, not asserted.** Every enforcement claim on this page is now checked at runtime on your machine, rather than being true of the maintainers' machine and assumed of yours. Your kernel ABI, your installed package version, your OS — the combination that actually runs is the one that gets tested.
- **Failure is loud.** A backend that cannot enforce reports it, at selection, in a message that names what it attempted and what happened. With `on_unsupported: error` it refuses to run at all.

The self-test costs two probes (a handful of short subprocess launches, tens of milliseconds) per process, cached against the backend. It is paid only by a run that resolves a real backend — a run that never touches the sandbox never pays it, and it is not on the chat startup path.

**Why two probes and not one assertion.** The axes need contradictory policies — the write probe sets `allow_subprocess: true` to isolate its axis from the syscall layer, and the spawn probe sets it to `false` because that flag is its subject — so no single launch can witness both. The two **checks** also fail independently: on Linux the write boundary is Landlock's and the spawn gate is seccomp's, so the filter can be dead while path rules work. A write-only check reports that host as sandboxed.

**Why both must pass, rather than keeping whichever one does.** The checks decompose; the protection does not. Landlock governs ordinary writes but has no `chmod` right at all, and path-based `truncate` is outside the handled set — so with seccomp absent, both are ungoverned. Measured on Linux 6.8, Landlock enforcing, filter absent: `open()` on a file outside `write_paths` was refused, while `os.truncate()` on that same file **succeeded and emptied it**. What refuses those syscalls is the default-deny filter, by omitting them from its allowlist (see `_EXCLUDED_UNGOVERNABLE` in `backends/seccomp.py`). Landlock-without-seccomp is therefore not a weaker sandbox but an incoherent one, and the spawn probe — by witnessing that the filter **loaded at all** — is what keeps that hole closed.

Each probe establishes a **positive control** before its deny: an action the policy *grants* must be seen to happen, or the probe reports the backend as unwitnessed rather than passing it. Without that, a wrap that ran nothing at all leaves no forbidden file either, and "nothing happened" reads exactly like "the deny fired". The spawn probe carries a second control — under `allow_subprocess: false`, a *non*-forking command must still run — because its mechanism is a default-deny syscall filter, and a filter that refuses everything and one that refuses exactly `fork` are otherwise indistinguishable.

**What it does not cover.** The probes witness the filesystem write boundary and the process-spawn gate, both through the command-level wrap. They do not exercise the network gate, `read_deny_paths`, or the one-shot `run()` path's separate preexec ruleset. A backend that passes has fired two denies — not proof of every deny it claims.

A third probe, `probe_network_enforcement` (#3030), witnesses the network gate the same way (a `connect()` to a loopback listener the probe's own process opens, attempted under `network: false`, with the same positive-control / non-networking-control / deny shape) but is deliberately kept OUT of the cached, production-gating suite above — folding a third axis into every backend resolution on every host is a wider blast radius than this fix needed, so it stays a directly-callable, CI-only probe (`scripts/sandbox_landlock_deny_gate.py`'s `network` deny arm) rather than part of `enforcement_self_test`. It witnesses `connect()`, not `socket()`-create: `socket`/`bind` are always allowed regardless of `network` (#3060 — [configure-sandbox.md](../../guide/for-users/configure-sandbox.md) documents the exception and why), so `socket()` succeeding no longer distinguishes an enforcing backend from a broken one.

**macOS 26.3+ and `SeatbeltBackend`**: `sandbox-exec` remains shipped in macOS 26.3. An SBPL profile that includes `(import "bsd.sb")` and `(allow process-exec*)` is sufficient for the backend to function. See the FP-0017 post-dogfood fix landing notes (commit `b477508`) for details.

## `reyn.yaml` configuration

```yaml
sandbox:
  backend: auto        # auto | seatbelt | landlock | noop
  on_unsupported: warn # warn | error | ignore
```

- `backend: auto` — let Reyn pick the best available backend for the current platform (recommended).
- `backend: noop` — explicitly opt out of enforcement (useful in CI environments where you audit via events but do not need enforcement).
- `on_unsupported: error` — fail workflow dispatch if the configured backend is unavailable. Use in production environments where enforcement is a hard requirement.

## Configuring the sandbox (operator config)

Sandbox configuration is **operator-level** — set in `reyn.yaml` or via CLI flags, not per-workflow or per-phase. See [`reyn.yaml` reference → `sandbox:`](../../reference/config/reyn-yaml.md) for the full config schema.

> **Phase-level `default_sandbox_policy` was removed.** Sandbox policy is agent-level operator configuration, not a per-phase workflow declaration — configure it in [`reyn.yaml sandbox.policy`](../../reference/config/reyn-yaml.md). When set, that policy is the deterministic policy for sandboxed ops + the `SandboxLayer` of the permission intersection (it wins over op-declared fields, so a workflow or the LLM cannot widen it); absent, the op-level fields govern. The `phase.md` frontmatter key is no longer parsed.

## See also

- [FP-0017](../../deep-dives/proposals/0017-sandboxed-execution.md) — design rationale, component history, and backend implementation details.
- [Control IR: `sandboxed_exec`](../../reference/runtime/control-ir.md#sandboxed_exec) — op schema and field reference.
- [Permission model](../runtime/permission-model.md) — dispatch-time declared-scope enforcement that the sandbox complements at runtime.
