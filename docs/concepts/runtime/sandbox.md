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
| `allow_subprocess` | `bool` | `true` | Allow the sandboxed process to spawn child processes. Enforced on Linux (seccomp) and macOS (Seatbelt: `process-fork` denied when off; the target's own exec still works via `process-exec*`). |
| `env_passthrough` | `list[str]` | `[]` | Environment variable names passed through to the subprocess (all others are stripped). `PATH` is always passed. |
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

A third probe, `probe_network_enforcement` (#3030), witnesses the network gate the same way (a `connect()` to a loopback listener the probe's own process opens, attempted under `network: false`, with the same positive-control / non-networking-control / deny shape) but is deliberately kept OUT of the cached, production-gating suite above — folding a third axis into every backend resolution on every host is a wider blast radius than this fix needed, so it stays a directly-callable, CI-only probe (`scripts/sandbox_landlock_deny_gate.py`'s `network` deny arm) rather than part of `enforcement_self_test`. It witnesses `connect()`, not `socket()`-create: `socket`/`bind` are always allowed regardless of `network` (#3060 — [configure-sandbox.md](../../guide/for-users/configure-sandbox.md) documents the exception and why), so `socket()` succeeding no longer distinguishes an enforcing backend from a broken one. #3060 also extended it with two more arms: a connected-socketpair self-pipe (NULL-address `sendto`/`recvfrom` — the async event loop's own wakeup) must SURVIVE, while an ADDRESSED `sendto` (real UDP egress) must stay DENIED — so the NULL-address allowance is proven neither too tight (the runtime pumps) nor too loose (egress refused).

### The axis contract — 1 bit → 3-tuple, and why production stays 1 bit (#2983)

The self-test above checks one bit per axis: did a deny fire. #3060's two extra `probe_network_enforcement` arms (the NULL-address self-pipe survival, the addressed-`sendto` deny) showed that one bit is not the whole claim for an axis that carries a deliberate exception, and #3060's `test_chunker_server_reaches_serving_under_network_false` showed a failure mode ("every syscall probe is green, the server still hangs") that "did a deny fire" is structurally blind to. `reyn.security.sandbox.axis_contract` generalises those two witness classes into a per-axis contract of **three independent legs**:

1. **deny** — the axis's core deny actually fires (what the self-test above already checks).
2. **boundary** — each declared exception (`AxisException`, e.g. network's NULL-addr `sendto`/`recvfrom` allowance) has its own probe proving it did not reopen the axis.
3. **workload** — the real workload the axis exists to gate reaches its intended state under the restriction (reachable-for-purpose, not merely "no syscall was refused unexpectedly").

`AxisException.boundary_probe` and `AxisContract.exceptions` have **no default value** — omitting either at construction is a `TypeError`, not a silently-empty exception or a forgotten leg. An axis not yet migrated onto the contract states `NOT_MIGRATED` explicitly on all four fields rather than being absent, and a CI test asserts the exact set of currently-migrated axis names, so a partially-migrated or silently-regressed axis cannot read as "done."

**This contract is deliberately NOT wired into `enforcement_self_test`.** That function is the production gate every real backend resolution calls; its blast radius is every sandboxed op on every host, and `probe_network_enforcement` is kept out of it for exactly that reason (a probe bug there would silently fall every op back to `NoopBackend`, not just fail to witness one axis). Widening that same gate to run all three legs for every axis would widen the blast radius of a probe bug in any future leg to the same degree. So the two layers stay split:

| Layer | What runs | Blast radius |
|---|---|---|
| **production gate** (`enforcement_self_test`) | deny leg only, write + spawn axes only — unchanged by the axis contract | every sandboxed op, every host |
| **CI conformance** (`tests/test_sandbox_axis_contract_2983.py`, Linux-only, gated like `sandbox_landlock_deny_gate.py`) | all three legs, for every migrated axis, against a real backend | CI only |

This is not a new pattern — `scripts/sandbox_landlock_deny_gate.py` (#2983 stage 3) already runs real deny arms as a CI-only gate, never a production one. The axis contract generalises that split into a typed per-axis registry instead of a fixed arm list. `network` was the first axis migrated (deny = #3030, boundary = #3060, workload = #3060's chunker-serving probe, reused rather than reimplemented). `write` and `spawn` are now migrated too: both deny legs reuse stage 1's own `probe_enforcement` (write) and `probe_subprocess_enforcement` (spawn) rather than a new implementation, both declare `exceptions=()` explicitly (neither axis carries a deliberate hole the way network's NULL-addr allowance does), and both workload legs are new, minimal tests added in `tests/test_sandbox_axis_contract_2983.py` (`test_write_workload_grant_write_succeeds`, `test_spawn_workload_permitted_child_process_launches`) — no pre-existing test witnessed "reachable for purpose" for either axis the way #3060's chunker test did for network. All three axes named in `AXIS_REGISTRY` are now migrated, and `_EXPECTED_MIGRATED_AXES` states that set explicitly (not derived from the registry) so the migration-count guard cannot pass vacuously.

The registry also records `witness_strength` per backend — network's deny leg is `BEHAVIORAL` (a real `connect()` attempt) on seccomp but only `PROFILE_TEXT` (SBPL text inspection, no real deny attempted) on Seatbelt. That asymmetry is not new, but it was previously unwritten; the axis contract makes it a recorded decision rather than an unnoticed gap. Adding real behavioral witnessing to Seatbelt is out of scope for this PR (a separate issue) — mixing a security-contract change with a witness-strength feature addition would dilute review of both. Write's and spawn's deny legs are `BEHAVIORAL` on both platforms they map (`landlock`/`seatbelt` for write, `seccomp`/`seatbelt` for spawn) — both probes execute a real `wrap_command()` launch and observe the filesystem, on either backend.

**The two strengths are not equivalent, and #3178 records why the gap is kept rather than closed.** `PROFILE_TEXT` verifies that the SBPL string reyn generated says the right thing; it does not verify that `sandbox-exec` enforced it that way — those are different claims. The asymmetry is accepted because of where each backend puts reyn's own code: Seatbelt hands a declaration to the OS's sandbox mechanism with almost no reyn code between declaration and enforcement, while seccomp has reyn build and load the BPF filter itself, so there is more reyn-authored surface that can be wrong — spending the stronger, real-behavior leg there is a deliberate allocation of verification effort, not a shortcut. The limit is real: #3060 could not determine from the SBPL text alone whether `(allow network-bind (local ip "localhost:*"))` covers IPv6 `::1` — that required checking behavior on actual darwin hardware, which is the honest reason `PROFILE_TEXT` is a cost/benefit call rather than a proof of equivalence. This repo's CI has no macOS runner (`.github/workflows/*.yml` is all `runs-on: ubuntu-latest`), so a Seatbelt behavioral test would only ever show up as a CI skip today — green without proving anything — which is why closing the gap is deferred rather than done now; revisit once a macOS runner exists in CI. A developer with local Mac hardware can already verify behaviorally by hand, as the `::1` question above was resolved that way.

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
