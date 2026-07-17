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
| `env_passthrough` | `list[str]` | `[]` | Environment variable names passed through to the subprocess (all others are stripped). `PATH` is always passed. |
| `timeout_seconds` | `int` | `60` | Wall-clock limit; process is killed on expiry. |

## Backend selection table

`get_default_backend(config)` selects a backend at runtime based on platform, installed extras, and — before handing the backend to any caller — **whether that backend actually enforces on this host**. The `sandbox.backend` config key in `reyn.yaml` overrides automatic selection.

| Platform | Condition | Backend | Notes |
|----------|-----------|---------|-------|
| macOS | `sandbox-exec` present **and self-test passes** | `SeatbeltBackend` | SBPL deny-default profile via `sandbox-exec`. `sandbox-exec` is deprecated upstream but remains functional on macOS 26.3. Falls back to `NoopBackend` if the binary is absent. |
| Linux | kernel ≥ 5.13, `sandbox-linux` extra installed, **self-test passes** | `LandlockBackend` + seccomp-BPF | `pip install reyn[sandbox-linux]` required. ABI v4+ adds network port rules. |
| Linux | kernel < 5.13 or `sandbox-linux` not installed | `NoopBackend` | Audit-only; no enforcement. |
| Any | a backend is present but **fails its self-test** | `NoopBackend` (per `on_unsupported`) | The mechanism is installed but does not enforce. Treated exactly like an absent one. |
| Other | any | `NoopBackend` | Audit-only; no enforcement. |

When `NoopBackend` is used, Reyn logs a one-line `WARN` on first invocation. Set `sandbox.on_unsupported: error` to hard-fail instead.

### The enforcement self-test

A backend is selected only if it **fired a real deny on this machine**. At resolution Reyn launches a short subprocess through the backend's own wrap and attempts a write to a path outside `write_paths` — a write every real backend must refuse. If the write succeeds, the backend does not enforce, and `sandbox.on_unsupported` applies as though the backend were absent.

This exists because "the mechanism is installed" and "the mechanism works" are different claims, and only the first was ever checked. A backend can be present, importable, and completely inert — so a check that asks only whether it is present will pass while nothing is enforced. The self-test asks the second question, on the host that makes the claim.

Two properties follow, and both matter more than the check itself:

- **It is verified, not asserted.** Every enforcement claim on this page is now checked at runtime on your machine, rather than being true of the maintainers' machine and assumed of yours. Your kernel ABI, your installed package version, your OS — the combination that actually runs is the one that gets tested.
- **Failure is loud.** A backend that cannot enforce reports it, at selection, in a message that names what it attempted and what happened. With `on_unsupported: error` it refuses to run at all.

The self-test costs one probe (two short subprocess launches, tens of milliseconds) per process, cached against the backend. It is paid only by a run that resolves a real backend — a run that never touches the sandbox never pays it, and it is not on the chat startup path.

**What it does not cover.** The probe witnesses the filesystem write boundary. It does not exercise the network gate, the `allow_subprocess` / seccomp syscall layer, or every path a policy governs. A backend that passes has fired one deny — not proof of every deny it claims.

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
