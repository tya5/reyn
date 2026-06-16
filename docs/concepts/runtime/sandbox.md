---
type: concept
topic: security
audience: [human, agent]
---

# Sandbox

Reyn's sandbox layer provides **operator-level containment** for subprocess execution. The operator configures the backend and scoping model via `reyn.yaml`; the OS enforces it without any OS code knowing which skill is running (P3 / P7). Sandbox is orthogonal to permissions — see [Sandbox and permissions: orthogonal concerns](../architecture/sandbox-vs-permission.md).

The sandbox complements the [permission model](../runtime/permission-model.md): permissions enforce declared scope at dispatch time (before the op runs); the sandbox enforces the same boundaries at the system-call level while the subprocess is running. The two layers are independent and additive.

## `SandboxPolicy` field reference

Defined in `src/reyn/security/sandbox/policy.py`. Passed as fields on a `sandboxed_exec` Control IR op.

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `network` | `bool` | `false` | Allow outbound network connections. The primary exfiltration gate. |
| `write_paths` | `list[str]` | `[]` | Filesystem paths the subprocess may write (tight guard). |
| `read_deny_paths` | `list[str]` | [OS credential paths] | Sensitive paths denied from the broad read surface (defense-in-depth). Enforced only on backends that support deny-after-allow (Seatbelt). Default: `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gcloud`, `~/.kube`, `~/.docker/config.json`, `~/.netrc`. |
| `read_paths` | `list[str]` | `[]` | **Legacy** — formerly the read allowlist. Under the current broad-read model reads are not restricted to this list; retained for backward compatibility and as documentation of intended read targets. |
| `allow_subprocess` | `bool` | `false` | Allow the subprocess to spawn child processes. Advisory under Seatbelt (process-fork always permitted for binary bootstrap). |
| `env_passthrough` | `list[str]` | `[]` | Environment variable names passed through to the subprocess (all others are stripped). `PATH` is always passed. |
| `timeout_seconds` | `int` | `60` | Wall-clock limit; process is killed on expiry. |

## Backend selection table

`get_default_backend(config)` selects a backend at runtime based on platform and installed extras. The `sandbox.backend` config key in `reyn.yaml` overrides automatic selection.

| Platform | Condition | Backend | Notes |
|----------|-----------|---------|-------|
| macOS | `sandbox-exec` available | `SeatbeltBackend` | SBPL deny-default profile via `sandbox-exec`. `sandbox-exec` is deprecated upstream but remains functional on macOS 26.3. Falls back to `NoopBackend` if the binary is absent. |
| Linux | kernel ≥ 5.13, `sandbox-linux` extra installed | `LandlockBackend` + seccomp-BPF | `pip install reyn[sandbox-linux]` required. ABI v4+ adds network port rules. |
| Linux | kernel < 5.13 or `sandbox-linux` not installed | `NoopBackend` | Audit-only; no enforcement. |
| Other | any | `NoopBackend` | Audit-only; no enforcement. |

When `NoopBackend` is used, Reyn logs a one-line `WARN` on first invocation. Set `sandbox.on_unsupported: error` to hard-fail instead.

**macOS 26.3+ and `SeatbeltBackend`**: `sandbox-exec` remains shipped in macOS 26.3. An SBPL profile that includes `(import "bsd.sb")` and `(allow process-exec*)` is sufficient for the backend to function. See the FP-0017 post-dogfood fix landing notes (commit `b477508`) for details.

## `reyn.yaml` configuration

```yaml
sandbox:
  backend: auto        # auto | seatbelt | landlock | noop
  on_unsupported: warn # warn | error | ignore
```

- `backend: auto` — let Reyn pick the best available backend for the current platform (recommended).
- `backend: noop` — explicitly opt out of enforcement (useful in CI environments where you audit via events but do not need enforcement).
- `on_unsupported: error` — fail skill dispatch if the configured backend is unavailable. Use in production environments where enforcement is a hard requirement.

## Configuring the sandbox (operator config)

Sandbox configuration is **operator-level** — set in `reyn.yaml` or via CLI flags, not per-skill or per-phase. See [`reyn.yaml` reference → `sandbox:`](../../reference/config/reyn-yaml.md) for the full config schema.

> **Phase-level `default_sandbox_policy` was removed.** Sandbox policy is agent-level operator configuration, not a per-phase skill declaration — configure it in [`reyn.yaml sandbox.policy`](../../reference/config/reyn-yaml.md). When set, that policy is the deterministic policy for sandboxed ops + the `SandboxLayer` of the permission intersection (it wins over op-declared fields, so a skill or the LLM cannot widen it); absent, the op-level fields govern. The `phase.md` frontmatter key is no longer parsed.

## See also

- [FP-0017](../../deep-dives/proposals/0017-sandboxed-execution.md) — design rationale, component history, and backend implementation details.
- [Control IR: `sandboxed_exec`](../../reference/runtime/control-ir.md#sandboxed_exec) — op schema and field reference.
- [Permission model](../runtime/permission-model.md) — dispatch-time declared-scope enforcement that the sandbox complements at runtime.
