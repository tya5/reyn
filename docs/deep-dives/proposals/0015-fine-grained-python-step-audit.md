# FP-0015: Fine-grained Per-call Python Step Audit (Bidirectional RPC)

**Status**: **deferred** (= awaiting concrete enterprise audit
requirement)
**Proposed**: 2026-05-11
**Author**: 2026-05-11 design discussion (split out of FP-0014's ADR-B)
**Trigger**: FP-0014 adopts Scope A (= subprocess-local helpers, step-
level audit only) because contextvars don't cross the `subprocess.run`
boundary used by `python_runner.py` → `reyn.kernel._python_harness`.
Scope B (= per-call audit) needs a bidirectional RPC channel between
parent and child; that's its own design question — separated here so
FP-0014 can ship without it.

---

## Summary

Extend `reyn.unsafe.*` helpers so each I/O call dispatches back through
the parent's run_op dispatcher instead of executing stdlib I/O locally
in the subprocess. The author-visible API (= `reyn.unsafe.file.read(...)`)
is unchanged; the wrapper body switches from `open(path).read()` to an
RPC that the parent fulfils via `dispatch_op("file", verb="read", ...)`.

Win: every individual I/O call gets the same audit treatment as a
declarative run_op step — per-call permission gate, event emission,
LLMReplay capture. Cost: a new bidirectional RPC protocol over the
parent/child JSON channel, and the per-call latency of a round-trip
through the parent.

---

## Motivation

FP-0014 Scope A's audit granularity is **step-level**: when a python
step starts, the parent emits `python_started` with the function name +
mode; when it returns, `python_completed` with the result. What happens
inside the subprocess is opaque to the parent's audit log. This matches
today's `mode: trusted` behaviour, so FP-0014 is not a regression.

But it doesn't meet enterprise audit requirements that ask:

- "Show me every file path this skill read."
- "Block this specific external URL even though the skill is approved
  for network access."
- "Reproduce this skill execution byte-for-byte from the audit log."

These need per-invocation visibility. Scope B opens that path.

## Proposed implementation

### Component A — Bidirectional JSON channel

Today's harness protocol is one-shot: parent writes a request to stdin,
reads a response from stdout, done. Extend to length-prefixed bidirectional
framing on a dedicated channel (= side socket / `pipe` pair / extra fd).
Stdin/stdout stay for the original request/response envelope (= backward
compat).

```
Frame format (each direction):
  4-byte big-endian length || JSON payload

Child → Parent (dispatch request):
  {"kind": "op_dispatch", "id": "...", "op": "file", "args": {...}}

Parent → Child (dispatch response):
  {"kind": "op_result", "id": "...", "ok": true, "result": ...}
  or
  {"kind": "op_result", "id": "...", "ok": false, "error": ..., "kind": ...}

Child → Parent (terminal):
  {"kind": "step_result", "ok": true, "result": ...}
  (same envelope as today's stdout response)
```

`id` correlates dispatch requests with results across concurrent in-flight
calls (= if a helper does parallel I/O via threads).

### Component B — In-child RPC client

`reyn.api._internal.dispatch_op` rewrites from local stdlib call to RPC:

```python
# Scope B: helper body changes from local stdlib call to dispatch RPC
def read(path: str, *, encoding: str = "utf-8") -> str:
    return _dispatch_op("file", verb="read", path=path, encoding=encoding)
```

The `_dispatch_op` implementation lives in `reyn.api._internal`,
serialises the request frame, writes to the RPC channel, blocks on the
response frame, raises on RPC error / op failure.

### Component C — Parent RPC server loop

The parent's `PythonRunner.run` extends from one-shot `subprocess.run` to
a streaming reader on the RPC channel concurrent with the child's
execution. Each `op_dispatch` frame:

1. Parses the frame.
2. Looks up the op kind in the existing `dispatch_op` machinery.
3. Calls `PermissionResolver.require_*` (= per-call permission gate).
4. Emits `op_started` / `op_completed` events (= per-call audit).
5. Writes the result frame back to the RPC channel.

Cancellation (= step timeout fires mid-RPC) cleanly kills the child.

### Component D — Permission gate per-call

Today's `python.unsafe` permission grants the **whole step**. With
Scope B, each `op_dispatch` triggers a `require_*` check on the
specific op + args. Two policy choices to decide in ADR:

- **Inherit-from-step**: child step already approved for `mode: unsafe`
  → all `op_dispatch` calls pass. Same effective granularity as Scope A
  but with audit emission per call.
- **Re-gate**: each `op_dispatch` re-checks against the skill's
  declared permissions (`permissions.file_read`, `permissions.http`,
  etc.). Strict granularity; matches a declarative run_op step.

Re-gate is more rigorous; inherit-from-step is simpler.

## Open design questions (delegate to ADR)

1. **ADR-A: Channel implementation.** Side socket, extra pipe pair via
   `subprocess.Popen(pass_fds=...)`, or multiplexed framing over
   stdin/stdout? `pass_fds` is portable across Linux/macOS, doesn't add
   sockets, gives clean separation.
2. **ADR-B: Permission gate policy.** Inherit-from-step vs re-gate
   (see Component D).
3. **ADR-C: Concurrency model.** Does the parent serve dispatch
   requests synchronously (= one RPC at a time) or concurrently (= the
   helper supports threads / async)? Concurrent is more capable but
   needs serialisable ordering for replay.
4. **ADR-D: LLMReplay integration.** Each `op_dispatch` should produce
   a replay-capturable record indexed by the same `(skill, phase, step,
   call_idx)` tuple that today's run_op records use. Schema migration
   needed.
5. **ADR-E: Backward compatibility with Scope A.** Can a single
   `reyn.unsafe.*` package serve both modes (= local stdlib vs RPC)
   based on parent capability advertisement, or do they ship as
   separate package versions?

## Dependencies

- **FP-0014 (= LANDED prerequisite)** — provides `reyn.unsafe.*`
  namespace + `reyn.api._internal.dispatch_op` abstraction point that
  Scope B replaces internally.
- **`PermissionResolver` per-call API** — already exists, no changes
  needed.
- **Events store schema** — `op_started` / `op_completed` schemas
  already exist for declarative run_op; reuse as-is.
- **LLMReplay capture** — extend to record per-call op invocations
  inside python steps.

## Cost estimate

**MEDIUM** (~3-4 days).

| Item | Estimate |
|---|---|
| Bidirectional JSON channel + framing | ~1 day |
| In-child `_dispatch_op` RPC client | ~0.5 day |
| Parent RPC server loop integration | ~1 day |
| Permission policy + event emission | ~0.5 day |
| LLMReplay schema migration | ~0.5 day |
| ADR drafting (A-E) | ~0.5 day |
| Migrate `reyn.unsafe.*` wrapper bodies | ~0.5 day |
| Tests (Tier 2 + Tier 3 e2e) | ~0.5 day |

Stdlib refactor does NOT need to change (= already running in safe mode
post-FP-0014). User-side `mode: unsafe` skills automatically get the
new audit granularity once the package version ships.

## Risks

- **Latency overhead** — every helper call pays a round-trip through
  the parent. Acceptable for I/O-heavy steps that do a handful of
  calls; bad for tight loops. Mitigation: keep Scope A as a
  per-skill opt-out (= `audit_level: step` vs `audit_level: per_call`).
- **Cancellation correctness** — step timeout firing mid-RPC must
  cleanly kill the child without leaking parent state. Mitigation:
  the channel is owned by parent and torn down on child kill.
- **Replay determinism** — concurrent RPCs need a total order capture
  so replay reproduces the same call sequence. ADR-C resolves this.

## When to revisit

**Trigger conditions** for promoting this proposal from `deferred` to
`accepted`:

- Concrete enterprise customer audit requirement that names
  per-invocation gating (= "we need to know every file path read").
- Security review finding that step-level audit is insufficient for
  Reyn's threat model.
- Replay reliability work that needs op-level replay inside python
  steps (= today's step-level replay can drift on non-deterministic
  helper internals).

Until one of these fires, FP-0014's Scope A is sufficient.

## Related

- **FP-0014 (= prerequisite, LANDED)** — adopts Scope A (= step-level
  audit); leaves `reyn.unsafe.*` namespace as the future hookup point
  this proposal extends.
- **ADR-0026 unified tool registry** — declarative run_op already has
  the per-call audit infrastructure this FP brings to python steps.
- **`docs/concepts/runtime/events.md`** — events emission model.
- **`docs/reference/testing/replay.md`** — LLMReplay scope; extends
  to op-level under this FP.
