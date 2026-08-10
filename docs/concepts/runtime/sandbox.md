---
type: concept
topic: security
audience: [human, agent]
---

# Sandbox

Reyn's sandbox layer provides **operator-level containment** for subprocess execution. The operator configures the backend and scoping model via `reyn.yaml`; the OS enforces it without any OS code knowing which workflow is running (P3 / P7). Sandbox is orthogonal to permissions — see [Sandbox and permissions: orthogonal concerns](../architecture/sandbox-vs-permission.md).

The sandbox complements the [permission model](../runtime/permission-model.md): permissions enforce declared scope at dispatch time (before the op runs); the sandbox enforces the same boundaries at the system-call level while the subprocess is running. The two layers are independent and additive.

## `SandboxPolicy` field reference

Defined in `src/reyn/security/sandbox/policy.py` — the dataclass every backend
(`Seatbelt`/`Landlock`/`Noop`) actually receives. The `sandboxed_exec` Control
IR op carries **no policy fields at all, and no `timeout_seconds`** (`#3907`
deleted the 5 policy fields it used to have — `network`/`read_paths`/
`write_paths`/`allow_subprocess`/`env_passthrough` — measured to have zero
real producers; the op-fields fallback path they fed was itself unreachable
in production, since every context-building path already resolves a concrete
policy. `#3962` deleted `timeout_seconds` for the same reason — it wasn't one
of the 5 #3907 scoped to, since a wall-clock cap isn't a permission axis, so
it survived that sweep dead one issue longer). The policy that actually
governs a run — including its timeout — is never settable via the op; it is
always the agent-level (operator) `sandbox.policy`, or absent that, the
operator's compat/strict default; see [Control IR:
`sandboxed_exec`](../../reference/runtime/control-ir.md#sandboxed_exec).

**These are `SandboxPolicy`'s own internal field names — not what an operator
writes in `reyn.yaml`.** `#3823` layered a separate, decoupled config
vocabulary on top (`allow_write_paths` / `deny_read_paths` / `deny_write_paths`
/ `subprocess` / `allow_env_names` / `deny_env_names`), translated into the
internal names below by `_translate_sandbox_policy_config` before construction
— see [`reyn.yaml` § `sandbox.policy` sub-keys](../../reference/config/reyn-yaml.md#sandbox-block)
or [Configure the sandbox](../../guide/for-users/configure-sandbox.md) for the
vocabulary you actually write. This table is the internal reference for
reading `policy.py` itself.

Every field except `write_paths` defaults to full compat (owner ruling B,
#3901): the sandbox's job is bounding what happens *behind* a permitted
action, not re-deciding what the launching shell could already do.

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `network` | `bool` | `true` (compat) | Allow outbound network connections. The primary exfiltration gate — still an operator-declared permission-∩ axis (not retired by #3901 ③, unlike the two path axes below): a config-allowed host is still denied under `network: false`. |
| `write_paths` | `list[str]` | `[]` | Filesystem paths the subprocess may write (tight guard) — the one field that stays closed by default (an operator-unknowable value the kernel backend consumes directly; no compat floor makes sense for it). Write implies read. `~` is expanded. |
| `read_deny_paths` | `list[str]` | `[]` (compat) | Sensitive paths denied from the broad read surface (defense-in-depth, **opt-in**). Enforced only on backends that support deny-after-allow (Seatbelt). Before #3901 this defaulted to 7 OS-level credential paths (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gcloud`, `~/.kube`, `~/.docker/config.json`, `~/.netrc`) — an operator (or a caller like the MCP client, which runs untrusted third-party code) now sets that list explicitly to get it back. Denies only the READ axis. |
| `write_deny_paths` | `list[str]` | `[]` | The write axis's own deny-list (#3901), mirroring `read_deny_paths`. Before #3901, Seatbelt denied writes as an undocumented side-effect of `read_deny_paths`; Landlock never replicated that side-effect, so the same policy meant different things per OS — this field closes that gap with one real field both backends read. Denies only the WRITE axis. |
| `deny_subprocess` | `bool` | `false` (compat) | Deny the sandboxed process from spawning children — the deny-list-shaped inverse of the pre-#3901 `allow_subprocess`. Enforced on Linux (seccomp) and macOS (Seatbelt: `process-fork` denied when `true`; the target's own exec still works via `process-exec*`). |
| `env_deny_names` | `list[str]` | `[]` (compat) | Environment variable names WITHHELD from the subprocess — the deny-list-shaped inverse of the pre-#3901 `env_passthrough` allowlist. Empty (the default) means the WHOLE environment passes through, the same trust level as the launching shell. |
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

A backend is selected only if it **fired a real deny on this machine**, on every axis it claims. At resolution Reyn launches short subprocesses through the backend's own wrap and attempts two things every real backend must refuse: a write to a path outside `write_paths`, and a process spawn under `deny_subprocess: true`. If either succeeds, the backend does not enforce what it advertises, and `sandbox.on_unsupported` applies as though the backend were absent.

This exists because "the mechanism is installed" and "the mechanism works" are different claims, and only the first was ever checked. A backend can be present, importable, and completely inert — so a check that asks only whether it is present will pass while nothing is enforced. The self-test asks the second question, on the host that makes the claim.

Two properties follow, and both matter more than the check itself:

- **It is verified, not asserted.** Every enforcement claim on this page is now checked at runtime on your machine, rather than being true of the maintainers' machine and assumed of yours. Your kernel ABI, your installed package version, your OS — the combination that actually runs is the one that gets tested.
- **Failure is loud.** A backend that cannot enforce reports it, at selection, in a message that names what it attempted and what happened. With `on_unsupported: error` it refuses to run at all.

The self-test costs two probes (a handful of short subprocess launches, tens of milliseconds) per process, cached against the backend. It is paid only by a run that resolves a real backend — a run that never touches the sandbox never pays it, and it is not on the chat startup path.

**Why two probes and not one assertion.** The axes need contradictory policies — the write probe sets `deny_subprocess: false` to isolate its axis from the syscall layer, and the spawn probe sets it to `true` because that flag is its subject — so no single launch can witness both. The two **checks** also fail independently: on Linux the write boundary is Landlock's and the spawn gate is seccomp's, so the filter can be dead while path rules work. A write-only check reports that host as sandboxed.

**Why both must pass, rather than keeping whichever one does.** The checks decompose; the protection does not. Landlock governs ordinary writes but has no `chmod` right at all, and path-based `truncate` is outside the handled set — so with seccomp absent, both are ungoverned. Measured on Linux 6.8, Landlock enforcing, filter absent: `open()` on a file outside `write_paths` was refused, while `os.truncate()` on that same file **succeeded and emptied it**. What refuses those syscalls is the default-deny filter, by omitting them from its allowlist (see `_EXCLUDED_UNGOVERNABLE` in `backends/seccomp.py`). Landlock-without-seccomp is therefore not a weaker sandbox but an incoherent one, and the spawn probe — by witnessing that the filter **loaded at all** — is what keeps that hole closed.

Each probe establishes a **positive control** before its deny: an action the policy *grants* must be seen to happen, or the probe reports the backend as unwitnessed rather than passing it. Without that, a wrap that ran nothing at all leaves no forbidden file either, and "nothing happened" reads exactly like "the deny fired". The spawn probe carries a second control — under `deny_subprocess: true`, a *non*-forking command must still run — because its mechanism is a default-deny syscall filter, and a filter that refuses everything and one that refuses exactly `fork` are otherwise indistinguishable.

**What it does not cover.** The probes witness the filesystem write boundary and the process-spawn gate, both through the command-level wrap. They do not exercise the network gate, `read_deny_paths`, or the one-shot `run()` path's separate preexec ruleset. A backend that passes has fired two denies — not proof of every deny it claims.

A third probe, `probe_network_enforcement` (#3030), witnesses the network gate the same way (a `connect()` to a loopback listener the probe's own process opens, attempted under `network: false`, with the same positive-control / non-networking-control / deny shape) but is deliberately kept OUT of the cached, production-gating suite above — folding a third axis into every backend resolution on every host is a wider blast radius than this fix needed, so it stays a directly-callable, CI-only probe (`scripts/sandbox_landlock_deny_gate.py`'s `network` deny arm) rather than part of `enforcement_self_test`. It witnesses `connect()`, not `socket()`-create: `socket`/`bind` are always allowed regardless of `network` (#3060 — [configure-sandbox.md](../../guide/for-users/configure-sandbox.md) documents the exception and why), so `socket()` succeeding no longer distinguishes an enforcing backend from a broken one. #3060 also extended it with two more arms: a connected-socketpair self-pipe (NULL-address `sendto`/`recvfrom` — the async event loop's own wakeup) must SURVIVE, while an ADDRESSED `sendto` (real UDP egress) must stay DENIED — so the NULL-address allowance is proven neither too tight (the runtime pumps) nor too loose (egress refused).

**A fourth, orthogonal witness: load-method independence (#3229, derived from #3227).** #3227's competitive research found sandboxes elsewhere that key confinement on *how* a binary is launched (an `execve`-argv hook, an `LD_PRELOAD` interposer on a named `exec*` symbol) — bypassable by invoking the ELF interpreter (`ld-linux`) directly, or by `mmap`-ing executable code and jumping to it (no `exec*()` at all). reyn's boundary is architecturally not that shape (Landlock is an LSM syscall-layer hook, seccomp-BPF attaches to the whole process's syscall table), but this repo's standing rule is that a suspected-good boundary gets a witness, not a read. `scripts/sandbox_load_method_witness_3229.py` (CI-only, same FATAL-not-skip shape as `sandbox_landlock_deny_gate.py`) confines a process under one policy (write-allowlist + `network: false`) and, for BOTH load methods, asserts the write-outside-grant and loopback-`connect()` denies still fire. It does not modify `enforcement_self_test` or any production path — pure CI-conformance evidence, orthogonal to the axis contract below.

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

### Deny-list visibility: `sandbox_axis_unenforced` — scope, not a general enforcement-gap report

A configured `read_deny_paths`/`write_deny_paths` entry is silently
unenforced on Landlock (allowlist-only — see the field reference above).
`unenforced_axes()` (`policy.py`) makes that ONE gap observable: when it
resolves against a deny-list-incapable backend, a `sandbox_axis_unenforced`
audit-event fires (paired with a WARN log, `#3949`), naming the axis and why
(`#3823` §4③).

**This is deny-list visibility only, not a general "does this backend
enforce everything configured" report (`#3951`).** A backend that simply
does not enforce an axis at all produces no `sandbox_axis_unenforced` event
and no WARN, because it isn't in the deny-list-incapable set
`unenforced_axes()` checks (that set names Landlock specifically, not "any
backend that under-enforces"). Two real examples, reached two different
ways:

- **`NoopBackend`** — the intentional no-enforcement fallback. Enforces none
  of `write_paths`/`network`/`subprocess` (its own docstring: argv is
  returned UNCHANGED; all other policy fields are recorded for audit only).
- **`DockerEnvironmentBackend`** (`sandbox.backend`'s container-mode path,
  `src/reyn/environment/container_backend.py`) — reached via the
  single-shared-sandbox-instance invariant (`#1200`): a caller that builds
  one Docker backend for both the FS seam (`environment_backend`) and the
  exec seam (`sandbox_backend`, `env_backend.py`'s own module docstring)
  passes it straight into `sandboxed_exec.py`'s `resolve_backend(ctx.
  sandbox_backend, ...)`, whose result flows to `unenforced_axes(backend.
  name, policy)` with `backend.name == "docker"` — the same call site the
  two Seatbelt/Landlock/Noop backends reach, just with an instance the
  name-based auto-selector never builds on its own. Docker enforces none of
  `write_paths`/`network`/`subprocess` either, while being a real,
  operator-selectable execution path — not merely a fallback state, which
  is what makes it the sharper of the two examples.

Both produce a clean `unenforced_axes()` report while enforcing nothing —
a clean/absent report from this mechanism does not mean every configured
axis is being enforced; it means Landlock's specific deny-list limitation
didn't fire on this call. A quiet run under either and a quiet run under
`SeatbeltBackend` are indistinguishable from this signal alone — the
per-backend table above, not this mechanism, is what answers "what does my
backend actually enforce." Whether to extend `unenforced_axes()`'s own
coverage to Docker is tracked in `#3951`/`#4039`.

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

> **Phase-level `default_sandbox_policy` was removed.** Sandbox policy is agent-level operator configuration, not a per-phase workflow declaration — configure it in [`reyn.yaml sandbox.policy`](../../reference/config/reyn-yaml.md). When set, that policy is the deterministic policy for sandboxed ops + the `SandboxLayer` of the permission intersection for the `network`/`subprocess`/`env` axes (it wins over op-declared fields, so a workflow or the LLM cannot widen it) — `allow_write_paths` (and the read/write deny-lists) do NOT participate in that intersection, since they are values an operator cannot know in advance and the kernel backend consumes them directly (#3901 PR-B ③); absent, the op-level fields govern. The `phase.md` frontmatter key is no longer parsed.

## See also

- [FP-0017](../../deep-dives/proposals/0017-sandboxed-execution.md) — design rationale, component history, and backend implementation details.
- [Control IR: `sandboxed_exec`](../../reference/runtime/control-ir.md#sandboxed_exec) — op schema and field reference.
- [Permission model](../runtime/permission-model.md) — dispatch-time declared-scope enforcement that the sandbox complements at runtime.
