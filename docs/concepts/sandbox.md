---
type: concept
topic: security
audience: [human, agent]
---

# Sandbox

Reyn's sandbox layer translates the policy a skill declares into kernel-level enforcement — without any OS code knowing which skill is running. This is a direct application of P3 (OS is the runtime engine) and P7 (OS code must not contain skill-specific strings): the skill declares *what* it needs; the OS selects *how* to enforce it.

The sandbox complements the [permission model](permission-model.md): permissions enforce declared scope at dispatch time (before the op runs); the sandbox enforces the same boundaries at the system-call level while the subprocess is running. The two layers are independent and additive.

## `SandboxPolicy` field reference

Defined in `src/reyn/sandbox/policy.py`. Passed as fields on a `sandboxed_exec` Control IR op.

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `network` | `bool` | `false` | Allow outbound network connections |
| `read_paths` | `list[str]` | `[]` | Filesystem paths the subprocess may read (glob patterns and `{{workspace}}` template OK) |
| `write_paths` | `list[str]` | `[]` | Filesystem paths the subprocess may write |
| `allow_subprocess` | `bool` | `false` | Allow the subprocess to spawn child processes |
| `env_passthrough` | `list[str]` | `[]` | Environment variable names passed through to the subprocess (all others are stripped) |
| `timeout_seconds` | `int` | `60` | Wall-clock limit; process is killed on expiry |

## Backend selection table

`get_default_backend(config)` selects a backend at runtime based on platform and installed extras. The `sandbox.backend` config key in `reyn.yaml` overrides automatic selection.

| Platform | Condition | Backend | Notes |
|----------|-----------|---------|-------|
| macOS | < 26 | `SeatbeltBackend` | SBPL profile via `sandbox-exec`. Deprecated upstream — Apple removing in macOS 26. |
| macOS | ≥ 26 (future) | `AppleContainerBackend` | Not yet implemented (Component E, deferred). Falls back to `NoopBackend`. |
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

## Declaring a sandbox policy in a skill

In a skill's `skill.md`, add a `sandbox:` block to any phase that runs `sandboxed_exec`:

```yaml
# skill.md excerpt
phases:
  - name: run_script
    instructions: |
      Run the analysis script.
    sandbox:
      read_paths:
        - "{{workspace}}/input"
      write_paths:
        - "{{workspace}}/output"
      network: false
      timeout_seconds: 120
```

The `{{workspace}}` template is expanded by the OS at runtime to the skill's workspace directory. Skill authors MUST NOT hardcode absolute paths — use `{{workspace}}` for all workspace-relative paths and `/usr/bin`, `/usr/lib`, etc. for system paths (those are automatically allowed on backends that need them for dylib loading).

## See also

- [FP-0017](../deep-dives/proposals/0017-sandboxed-execution.md) — design rationale, component history, and backend implementation details.
- [Control IR: `sandboxed_exec`](../reference/runtime/control-ir.md#sandboxed_exec) — op schema and field reference.
- [Permission model](permission-model.md) — dispatch-time declared-scope enforcement that the sandbox complements at runtime.
