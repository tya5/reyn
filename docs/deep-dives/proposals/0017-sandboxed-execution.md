# FP-0017: Sandboxed Execution — Policy/Backend Abstraction and exec Op Deprecation

**Status**: **Components A + B + C + D landed 2026-05-15** (commit `ddf2d05` + this wave);
Component E is deferred
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)

## Landing notes (2026-05-11)

**Component A — `SandboxPolicy` + `SandboxBackend` Protocol + `sandboxed_exec`
op + `NoopBackend`** landed in commit `ddf2d05`:

- `src/reyn/sandbox/` package: `SandboxPolicy` dataclass (= network,
  read_paths, write_paths, allow_subprocess, env_passthrough, timeout_seconds),
  `SandboxBackend` Protocol, `SandboxResult` dataclass, `NoopBackend`
  (= default, no enforcement, one-shot WARN), `get_default_backend()`
  factory.
- `src/reyn/op_runtime/sandboxed_exec.py` op handler emitting
  `sandboxed_exec_started` / `sandboxed_exec_completed` events.
- `SandboxedExecIROp` schema entry + `OP_KIND_MODEL_MAP` registration
  with `OpPurity.external`.
- `docs/reference/runtime/control-ir.md` updated per CLAUDE.md sync rule.
- 13 Tier 2 tests.

**Component D — `shell` op deprecation** (= the actual analogue of the
proposal's `exec` op; the FP doc named `exec` but no such op exists in
the codebase — `shell` op is the closest match with the same "raw
subprocess, no isolation" semantics). The deprecation landed alongside
A: module docstring notice, one-time `DeprecationWarning` per skill on
first invocation, code comment "Deprecated by FP-0017. Will be removed
in 1.0 release. Use sandboxed_exec instead." Op remains functional;
zero test regressions.

**State as of 2026-05-11** — Components B / C remained proposed at that point (see the 2026-05-15 landing notes below).

---

## Landing notes (2026-05-15)

Components B and C landed in this wave.

**Component C — `SeatbeltBackend` (macOS `sandbox-exec` SBPL wrapper)**

Deny-default SBPL profile generated from `SandboxPolicy`, passed to `sandbox-exec -f`:

- Generates SBPL automatically from `SandboxPolicy` fields — `read_paths` maps to `(allow file-read-data (subpath "..."))`, `write_paths` to `(allow file-write-data (subpath "..."))`.
- Resolves all paths to absolute paths (`{{workspace}}` is expanded by the OS at runtime).
- Automatically allows `/usr/lib`, `/System/Library`, `/usr/bin`, `/bin`, `/usr/share`, etc. read-only for dylib loading.
- Available iff `platform.system() == "Darwin"`, `sandbox-exec` binary on PATH, and macOS < 26. Falls back to `NoopBackend` otherwise.
- Internally marked deprecated (Apple is removing `sandbox-exec` in macOS 26) — emits a runtime WARN on first use prompting migration to the future `AppleContainerBackend`.
- Files: `src/reyn/sandbox/backends/seatbelt.py`, `tests/test_sandbox_seatbelt.py`.

**Component B — `LandlockBackend` (Linux 5.13+)**

Filesystem + network restriction backend for Linux kernel 5.13+:

- Enabled via the `sandbox-linux` optional extra (`pip install reyn[sandbox-linux]`), using the `landlock` PyPI package.
- Detects ABI version (v1–v4) at startup and enables only features the running kernel supports (graceful degradation for kernels in the v1+ range).
- Applies `LANDLOCK_RULE_PATH_BENEATH` rules for `read_paths` / `write_paths`.
- Network restriction (`LANDLOCK_RULE_NET_PORT`) available on ABI v4+ (Linux 6.7+) only.
- **Contributor-friendly track**: the primary maintainer's dev environment is macOS-only; Linux contributors are welcome to validate end-to-end.
- Files: `src/reyn/sandbox/backends/landlock.py`, `tests/test_sandbox_landlock.py`.

**Component B (seccomp portion) — syscall filter builder**

seccomp-BPF layer stacked on top of `LandlockBackend`:

- Same `sandbox-linux` extra as `LandlockBackend` (`pyseccomp` package).
- Default-deny posture with a baseline allowlist sourced from Docker/Firejail defaults.
- Extends the allowlist based on `policy.network` and `policy.allow_subprocess`.
- Destructive filesystem syscalls (covered by Landlock) and known escape hatches (`ptrace`, `process_vm_readv`, etc.) are on the deny list unconditionally.
- Landlock and seccomp-BPF are orthogonal: Landlock enforces path/port restrictions; seccomp-BPF reduces the syscall surface (e.g. blocks `ptrace` which Landlock cannot).
- Files: `src/reyn/sandbox/backends/seccomp.py`, `tests/test_sandbox_seccomp.py`.

**Backend auto-selection + `SandboxConfig`**

`get_default_backend(config)` does lazy platform-aware backend selection:

- Configured via the `sandbox:` section in `reyn.yaml` (`backend: auto|seatbelt|landlock|noop`, `on_unsupported: warn|error|ignore`).
- `auto` inspects the platform and installed extras to select the best backend. See [concepts/sandbox.md](../../concepts/sandbox.md) for the selection table.
- `on_unsupported: error` fails skill dispatch when the requested backend is unavailable (for production environments requiring enforcement guarantees).
- Files: `src/reyn/config.py`, `src/reyn/sandbox/__init__.py`, `tests/test_sandbox_factory.py`.

---

## Summary

Reyn currently executes shell commands via the `exec` op as a direct `subprocess.run()` call
with full user privileges — no filesystem isolation, no network restriction, no resource
limits. The Permission model constrains *what skills declare they will do*, but provides no
runtime enforcement if a malicious prompt injection or buggy skill attempts destructive
operations outside declared scope. This proposal introduces a `SandboxPolicy` / `SandboxBackend`
abstraction that separates policy declaration (what a skill is allowed to do) from mechanism
selection (how the OS enforces it), along with a new `sandboxed_exec` op and immediate
deprecation of the unguarded `exec` op.

---

## Motivation

### Current state — `exec` op with no runtime isolation

The `exec` op in `src/reyn/op_runtime/exec.py` calls `subprocess.run()` with full user
privileges. Skills declare intent via the Permission model (ADR-0029), but the OS performs
no runtime enforcement at the system boundary. A prompt injection attack embedded in
processed content — a document, a web page, a code review diff — can instruct a skill to
run arbitrary shell commands. The Permission model will record the violation in P6 events
after the fact, but cannot prevent it.

A code audit of all stdlib skills confirms **zero skills currently use the `exec` op**. This
means the `exec` op can be deprecated immediately once a sandboxed replacement exists, with
no migration cost.

### Why sandboxing is a runtime OS concern (not a skill concern)

Reyn's principle P3 establishes the OS as the runtime enforcement layer — skills describe
what they need, the OS decides how to enforce it. Sandboxing is the natural extension of
this principle to the system-call boundary. Skills already declare filesystem paths and
network access in the Permission model; the sandbox policy is just the enforcement layer
that makes those declarations binding at the kernel level.

P7 requires that OS code contain no skill-specific strings. A sandbox policy expressed as
data (YAML in `skill.md`) keeps the mechanism in OS code while keeping policy declarations
in skill space — a clean boundary.

### Backend landscape and the case for abstraction

Sandboxing technologies are evolving rapidly:

- **Landlock** (Linux): in-kernel filesystem and network restriction. ABI has reached
  version 9 as of kernel 6.10. Stable for production since Linux 5.13 (ABI v1).
- **macOS sandbox-exec / SBPL**: mature but deprecated upstream. Apple is replacing it with
  Apple Containers in macOS 26.
- **Apple Containers** (macOS 26+): the successor to sandbox-exec. API is not yet finalized.
- **seccomp-BPF** (Linux): syscall surface reduction, orthogonal to Landlock. Can be stacked.
- **WASM runtimes**: isolation via bytecode, separate execution model. Out of scope for this FP.

Locking the `sandboxed_exec` op to any single backend today would cause breakage on the
first macOS major release that removes sandbox-exec (expected macOS 26). The abstraction
layer (`SandboxBackend` Protocol) insulates op code from backend churn.

### Design inspiration

- **OpenBSD `pledge`/`unveil`**: minimal capability declaration by the program itself,
  mechanism-agnostic kernel enforcement. Skills declaring `sandbox:` policy in `skill.md`
  is the same pattern at the OS/skill boundary.
- **systemd `PrivateTmp`/`ReadOnlyPaths`**: declarative policy mapped to kernel mechanisms.
  The `SandboxPolicy` YAML mirrors systemd service unit declarations.
- **Principle of least privilege**: a skill that processes untrusted documents should be
  able to declare "I only need to read `{{workspace}}/input/` and write `{{workspace}}/output/`"
  and have the OS enforce that boundary without any action from the LLM.

### Why not Docker

Docker requires a resident daemon process (`dockerd`) running as root. Landlock and
sandbox-exec are in-process, zero-overhead, require no daemon, and add no process startup
latency. Docker-style full container isolation is a valid future option (especially relevant
for `AppleContainerBackend`) but is explicitly out of scope for this FP. The `SandboxBackend`
Protocol is designed to accommodate a future `DockerBackend` if needed.

---

## Proposed implementation

### Abstraction layers

The design has two layers:

```
SandboxPolicy (what is allowed)     ← declared in skill.md
    ↓
SandboxBackend (how it's enforced)  ← selected by OS based on platform/kernel
```

This mirrors Reyn's existing Permission model structure (P3/P7): skills declare intent, the
OS enforces. The sandbox policy is a runtime-enforced extension of the existing permission
declaration.

### Component A — `SandboxPolicy` schema + `SandboxBackend` Protocol + `sandboxed_exec` op (SMALL)

**Policy schema** (declared in `skill.md`):

```yaml
sandbox:
  fs:
    - path: "{{workspace}}"   # template variable, resolved at runtime
      ops: [read, write]
    - path: "/usr/bin"
      ops: [execute]
  net:
    deny: all   # or allow: [{host: "api.example.com", port: 443}]
  resources:
    max_cpu_sec: 30
    max_memory_mb: 512
```

**Backend Protocol** (`src/reyn/sandbox/backend.py`):

```python
class SandboxCapability(Enum):
    FS_RESTRICT = "fs_restrict"
    NET_RESTRICT = "net_restrict"
    RESOURCE_LIMITS = "resource_limits"

class SandboxBackend(Protocol):
    def supports(self) -> set[SandboxCapability]: ...
    def apply(self, policy: SandboxPolicy) -> None: ...
```

**`sandboxed_exec` op**: a new Control IR op that requires a `SandboxPolicy`. The OS
selects the appropriate backend, calls `backend.apply(policy)`, then spawns the subprocess
inside the restricted environment. P6 events emitted: `sandbox_applied` (on successful
policy application), `sandbox_violation` (if the subprocess attempts an action outside the
declared policy).

The existing `exec` op gains a deprecation warning on use pointing callers to `sandboxed_exec`.

Target files:
- `src/reyn/sandbox/policy.py` — `SandboxPolicy` dataclass + `SandboxCapability` enum
- `src/reyn/sandbox/backend.py` — `SandboxBackend` Protocol + auto-selection logic
- `src/reyn/op_runtime/sandboxed_exec.py` — `sandboxed_exec` op handler
- `src/reyn/op_runtime/exec.py` — add deprecation warning
- `src/reyn/events/events.py` — `sandbox_applied`, `sandbox_violation` event payloads
- `docs/reference/runtime/control-ir.md` — `sandboxed_exec` op section (**NEVER rule: must
  be updated in the same PR as `sandboxed_exec` op registration in `OP_KIND_MODEL_MAP`**)

### Component B — `LandlockBackend` (MEDIUM) — contributor-friendly

> **Note**: The primary maintainer develops on macOS only. Component B cannot be verified
> without a Linux environment (Docker or Linux CI such as GitHub Actions `ubuntu-latest`).
> This component is explicitly marked as **contributor-friendly** — Linux contributors are
> welcome to implement and verify this backend independently against the `SandboxBackend`
> Protocol defined in Component A.

Linux 5.13+ backend. Uses the `landlock` PyPI package (supports ABI versions 1–4).

```python
class LandlockBackend(SandboxBackend):
    # Filesystem path rules via landlock_add_rule(LANDLOCK_RULE_PATH_BENEATH)
    # TCP port rules via landlock_add_rule(LANDLOCK_RULE_NET_PORT) — ABI v4+
    # Stacked with seccomp-BPF for syscall surface reduction (orthogonal coverage)
    ...
```

Auto-selection: Linux kernel ≥ 5.13 → `LandlockBackend`. Detects available ABI version at
runtime and enables only the capabilities the running kernel supports (degrades gracefully
on older ABI versions within the 5.13+ range).

seccomp-BPF is stacked on top of Landlock: Landlock handles path/port restrictions;
seccomp-BPF restricts the syscall surface. These are orthogonal — Landlock cannot block
`ptrace`, seccomp-BPF can.

Target files:
- `src/reyn/sandbox/backends/landlock.py` — `LandlockBackend`
- `src/reyn/sandbox/backends/seccomp.py` — seccomp-BPF filter builder (used by Landlock backend)

### Component C — `SeatbeltBackend` (SMALL)

macOS backend wrapping `sandbox-exec` with a generated SBPL (Sandbox Policy Language) profile
derived from `SandboxPolicy`. Covers filesystem allow/deny rules and network access rules.

```python
class SeatbeltBackend(SandboxBackend):
    # Generates a .sb profile from SandboxPolicy
    # Invokes subprocess via: sandbox-exec -f <profile> <cmd>
    # Marked as deprecated upstream (Apple removing in macOS 26)
    ...
```

Auto-selection: macOS < 26 → `SeatbeltBackend`. Marked internally as deprecated; a runtime
warning is logged noting that `AppleContainerBackend` will replace it on macOS 26+.

Target files:
- `src/reyn/sandbox/backends/seatbelt.py` — `SeatbeltBackend`
- `src/reyn/sandbox/backends/noop.py` — `NoopBackend` (fallback with warning; used on
  unsupported platforms)

### Component D — Deprecate `exec` op (TINY)

Add a `DeprecationWarning` to `src/reyn/op_runtime/exec.py` on every invocation:

```
DeprecationWarning: The `exec` op is deprecated and will be removed in the next major version.
Use `sandboxed_exec` with an explicit SandboxPolicy. Zero stdlib skills use `exec` — no
migration cost applies to stdlib. Custom skills should migrate to `sandboxed_exec`.
```

Schedule removal in next major version. No stdlib migration needed — zero stdlib skills use
`exec`.

Target files:
- `src/reyn/op_runtime/exec.py` — deprecation warning

### Component E — `AppleContainerBackend` (LARGE, deferred)

macOS 26+ backend using Apple Containers as the isolation primitive. Deferred until macOS 26
ships and the container API is finalized. The `SandboxBackend` Protocol is designed to
accommodate this backend without OS code changes.

Auto-selection (future): macOS ≥ 26 → `AppleContainerBackend` (replaces `SeatbeltBackend`).

### Auto-selection logic

`reyn.yaml` default: `backend: auto`.

| Platform | Condition | Selected backend |
|---|---|---|
| Linux | kernel ≥ 5.13 | `LandlockBackend` (+ seccomp stacked) |
| Linux | kernel < 5.13 | `SeccompOnlyBackend` |
| macOS | < 26 | `SeatbeltBackend` (deprecated upstream) |
| macOS | ≥ 26 (future) | `AppleContainerBackend` |
| Other | any | `NoopBackend` + warning |

**Configuration** (`reyn.yaml`):

```yaml
sandbox:
  backend: auto          # auto | landlock | seatbelt | none
  on_unsupported: warn   # warn | error | ignore
```

`on_unsupported: error` causes skill dispatch to fail if the requested backend is unavailable,
useful for production environments that require enforcement guarantees.

---

## Priority ordering

**A → D → C → B → E**

Component A (protocol + new op) is the foundation everything else builds on. Component D
(deprecation warning) can land with A at zero cost. Component C (Seatbelt) ships next because
macOS is the primary development environment. Component B (Landlock) covers Linux deployment
targets. Component E is deferred pending macOS 26 availability.

---

## Alignment with Reyn principles

| Principle | How this FP aligns |
|---|---|
| P3 | OS selects the backend; skills only declare policy. The LLM never touches enforcement mechanism selection. |
| P5 | Workspace path is the natural allow-list root for FS rules; `{{workspace}}` resolves to the OS-managed workspace at runtime. |
| P6 | `sandbox_applied` and `sandbox_violation` events preserve full audit trail of enforcement actions. |
| P7 | Backend code contains no skill-specific strings; `SandboxPolicy` is passed as data. Auto-selection logic references platform/kernel facts, not skill names. |
| P8 | Phase instructions describe what the skill needs; the sandbox enforcement mechanism is never described in Phase instructions. |

---

## Dependencies

- **None for Components A, B, C, D** — standalone additions to the op runtime
- **Component E**: macOS 26 release and stable Apple Containers API
- **CLAUDE.md NEVER rule**: `docs/reference/runtime/control-ir.md` must be updated in the
  same PR as `sandboxed_exec` op registration in `OP_KIND_MODEL_MAP` in
  `src/reyn/op_runtime/registry.py`

---

## Cost estimate

**Total active work: MEDIUM**

| Component | Cost | Notes |
|---|---|---|
| A: Policy schema + Backend Protocol + `sandboxed_exec` op | SMALL | New module + op handler + 2 P6 events |
| B: `LandlockBackend` | MEDIUM | `landlock` PyPI + seccomp-BPF stacking; ABI version detection |
| C: `SeatbeltBackend` + `NoopBackend` | SMALL | SBPL profile generator; straightforward wrapper |
| D: Deprecate `exec` op | TINY | One-line warning addition |
| E: `AppleContainerBackend` | LARGE | Deferred — macOS 26 required |
| Tests | SMALL | Tier 1: `sandboxed_exec` op contract; Tier 2: backend auto-selection invariant |

Component E is excluded from the active cost estimate because it is explicitly deferred.

---

## Related

- `src/reyn/op_runtime/exec.py` — current `exec` op (Component D: deprecation)
- `src/reyn/op_runtime/registry.py` — `OP_KIND_MODEL_MAP` (Component A: register `sandboxed_exec`)
- `src/reyn/sandbox/policy.py` — new file (Component A)
- `src/reyn/sandbox/backend.py` — new file (Component A)
- `src/reyn/sandbox/backends/landlock.py` — new file (Component B)
- `src/reyn/sandbox/backends/seatbelt.py` — new file (Component C)
- `src/reyn/sandbox/backends/noop.py` — new file (Component C)
- `src/reyn/op_runtime/sandboxed_exec.py` — new file (Component A)
- `src/reyn/config.py` — `SandboxConfig` (backend + on_unsupported)
- `src/reyn/events/events.py` — `sandbox_applied`, `sandbox_violation`
- `docs/reference/runtime/control-ir.md` — `sandboxed_exec` op reference
- ADR-0029 — Permission model (existing declaration layer this FP extends to enforcement)
- FP-0012 (`0012-async-skill-execution.md`) — async execution; sandboxing is especially
  important for long-running tasks that process untrusted input
