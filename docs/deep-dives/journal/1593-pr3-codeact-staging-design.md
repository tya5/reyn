# #1593 PR-3 — CodeAct scheme: staging design (pre-build, for design-review)

**Author:** dogfood-coder · **Status:** design-review gate (lead + e2e seam co-vet) · **Base:** `feat/1593-pr3-codeact` off `f093d3f2` (PR-1 merged)

Owner has **security-signed-off** the CodeAct model (`既存再利用の方向で confirm`). This
doc is the **build-front gate** lead required: *flow-trace-first (load-bearing OS-core)
→ staging → design-review → build*. Security model + correspondence table are settled
(#1593 issuecomment-4700512872 + addenda); this doc adds the two build-phase decisions
the flow-trace surfaced and the single-owned shared seam lead flagged.

---

## 1. Flow-trace (primary evidence — load-bearing OS-core seams PR-3 rides)

Traced on the merged base. Each seam read directly (not inferred):

### Seam 1 — `sandboxed_exec` op handler (`op_runtime/sandboxed_exec.py`)
```
handle(op, ctx, caller):
  backend = ctx.sandbox_backend or get_default_backend(ctx.sandbox_config)   # FP-0008 C7#2
  policy  = SandboxPolicy(**ctx.default_sandbox_policy)  if set              # #1326 operator WINS over op
            else SandboxPolicy(network/read/write/allow_subprocess/.../timeout from op)
  cwd     = ctx.workspace.base_dir
  emit sandboxed_exec_started (ACTUAL enforced policy values, #1339)         # P6
  result  = await backend.run(argv, policy, cwd=cwd, cancel_event=ctx.cancel_event)
  emit sandboxed_exec_completed | sandboxed_exec_cancelled (#1470)           # P6
  return {kind, status, backend, returncode, stdout, stderr, truncated}
```
**Reuse for PR-3:** the SandboxBackend selection + `#1326` operator-policy-wins + the
P6 event shape + cancel_event plumbing are all directly reusable.

### Seam 2 — `python_runner` + `_python_harness` (the subprocess harness)
- `PythonRunner.run(...)` builds a **JSON request** (`module_path`, `function`, `mode`,
  `artifact`, `allowed_modules`, `file_read_paths`, `file_write_paths`, `http_hosts`,
  `sandbox_write_paths`) → `argv = [py, -m, reyn.kernel._python_harness]`.
- Backend route (#1352-B): `sandbox_backend` real+available+not-noop → `_run_via_backend`
  (harness argv through `backend.run`, OS-sandboxed) else `_run_direct` (unsandboxed).
- **Both paths use `subprocess.run(..., capture_output=True)` / `backend.run` →
  single-shot**: stdin in, stdout captured *after the child exits*. There is **no
  interleaved read during execution**.
- Harness (`_python_harness.main`): `_read_request()` (stdin once) → `_exec_user_module`
  (AST-validate in `safe` mode + restricted `__builtins__` via `_build_restricted_builtins`)
  → push static permission contexts (`reyn.safe.file/http/embed_index._set_*_context`
  from the forwarded allowlists) → `fn(deepcopy(artifact))` → `_write_response()` (stdout
  once) → exit.

**Two structural facts that drive PR-3:**
1. **The harness is a pure one-shot** — request in, result out, no parent callback. (As
   lead's correspondence states: this is *why* the permission-proxy IPC is the only new
   mechanism.)
2. **The existing `safe.*` gating is "push static allowlist, then run"** — paths/hosts
   known up-front, self-gated *in the child*. This model **cannot express dynamic
   tool-call gating** (the tool + args aren't pre-declarable; the gate is the parent's
   `permission_resolver`, which lives in the parent OS per P5).

### Seam 3 — `dispatch_tool` (the parent gate — `dispatch/dispatcher.py`)
```
dispatch_tool(*, name, args, ctx: DispatchContext, invoker, op_invocation_id=None) -> dict
  → {status:"ok", data:...} | {status:"error", error:{kind, message}}
  kinds: unknown_tool | invalid_args | permission_denied | exception
  emits P6: tool_called / tool_returned / tool_failed (+ result/args_hash for resume-replay)
  PermissionError inside invoker → "permission_denied"
```
**This is the gate the permission-proxy calls.** The child never holds permission
authority; every effect round-trips to this parent function (P5 preserved unchanged).

---

## 2. Build-phase decision A — IPC transport (resolves my prior open question)

My prior design (#1593) left an open question: *"IPC mechanism: stdin/stdout JSON-RPC
over the existing harness pipe vs a socket — lean JSON-RPC over the existing pipe."*

**The flow-trace falsifies the "existing pipe" lean.** The harness stdin/stdout pipe is
consumed **single-shot** by `subprocess.run(capture_output=True)` / `backend.run` — stdout
is read only *after* the child exits. Real CodeAct interleaves computation with
**synchronous mid-execution `tool()` calls**, so it needs a **duplex channel live during
execution**. The existing one-shot pipe cannot carry it.

**Recommendation: a dedicated duplex side-channel (socketpair), serviced concurrently.**
```
parent: parent_sock, child_sock = socket.socketpair()
        launch harness via Popen(pass_fds=[child_sock.fileno()],  # inherited, NOT stdin/stdout
                                  env={REYN_CODEACT_CTRL_FD: <n>})
        await asyncio.gather(
            _wait_child(proc),                  # collects final stdout/stderr/returncode
            _service_control_socket(parent_sock))   # loop: read {tool,args} → dispatch_tool → write result
child:  tool(name, **args): marshal {tool,args} over fd → block on reply → return result.data
                            (the ONLY world-effect path; raw internals absent from namespace)
```
**Why a dedicated CodeAct execution path, NOT extending `backend.run`:** the single-shot
`backend.run` is load-bearing for `sandboxed_exec` + python preprocessor steps. Adding a
duplex-callback arm to that Protocol forces every backend (seatbelt/landlock/noop) to grow
a concurrent-service surface — destabilizing the stable interface for a single consumer.
A dedicated `CodeActRunner` **reuses** SandboxPolicy + the profile/`pass_fds` spawn
helpers + the harness namespace-injection pattern, while **localizing** the duplex
complexity. (Sandbox compatibility: an inherited socket fd survives Seatbelt — inherited
fds are usable by default — and Landlock — its rules govern *filesystem* paths, not an
already-open socket. The fd is the single, audited hole; it carries **only** marshalled
tool-call requests, each re-gated by the parent's exclude + permission + `dispatch_tool`
pipeline.)

> **Design-review question 1 (lead):** confirm the dedicated `CodeActRunner` (duplex
> socketpair + concurrent parent service loop, reusing policy/profile/harness-namespace)
> over extending `SandboxBackend.run` with a callback arm. This is the load-bearing
> transport decision; everything else is wiring.

---

## 3. Build-phase decision B — the 3-arm match (lead's single-ownership flag)

Lead flagged: my Execute-only note is the **shared seam** between PR-3 (CodeBlock) and
PR-4 (RePresent). To avoid a 3-way conflict / double-patch, **PR-3 single-owns** the
clean generalization of `_run_scheme_tool_round` from the current
`assert isinstance(interp, Execute)` to a **3-arm tagged-union match**:

```python
interp = self._scheme.interpret(llm_response, tool_catalog=self._catalog, ops=self)
match interp:
    case Execute():    # today's path — byte-identical (resolve→exclude-gate→dispatch→feedback)
        ...
    case CodeBlock():  # PR-3 CodeAct — run via CodeActRunner; each proxied call re-enters
                       #   the SAME OS exclude+permission+dispatch_tool pipeline (per-call)
        ...
    case RePresent():  # PR-4 — bounded re-present loop
        ...
```
**RePresent arm ownership — AGREED with e2e** (seam arbitration, #1593 issuecomment-4700737708 §2 + broker 04:54): PR-3 lands the **3-arm match structure only**; the RePresent **contract is owned entirely by PR-4**, so PR-3 does **not** pre-shape the RePresent field signature (avoids guessing the field shape). PR-3's RePresent arm is the minimal unreached-assert:
```python
case RePresent():
    # PR-4 owns the body + the RePresent contract. No PR-3-era scheme
    # (universal / enumerate-all / CodeAct) emits RePresent, so this is
    # genuinely unreachable until PR-4 lands the retrieval scheme.
    raise AssertionError("RePresent not reached (PR-4)")
```
PR-4 then replaces this arm body (single-arm, single-file change — zero structural churn,
no later-overwrite) with the bounded re-present loop. e2e's committed contract for PR-4:
`interp.refinement.candidates: Iterable[hashable]` (the scheme's current candidate set);
the arm loops `present → LLM → interpret` accumulating a monotonic `presented`/`seen` set
until convergence. **Bounded-by-construction** (monotonic `seen` over a finite action
space — no magic retry cap). e2e seam-expert co-vets PR-3's 4-method + exec_ctx coherence
when the PR opens.

> **Design-review question 2 (e2e): AGREED** — 3-arm match + `RePresent` =
> `raise AssertionError("RePresent not reached")` stub in PR-3; PR-4 owns the contract +
> body. Boundary accepted (this doc reflects it).

---

## 4. CodeAct scheme — the 4 `ToolUseScheme` methods (security model unchanged)

| Method | CodeAct behavior |
|---|---|
| `build_presentation` | render permission-eligible tools as a **code-API** (Python fn signatures + docstrings) in the SP; **excluded tools omitted** (#1400 mirror, defense-in-depth); `llm_tools_payload` native-minimal/empty. |
| `interpret` | extract the `CodeBlock` from the LLM response (tagged union, already in PR-1 IF). |
| `execute` | run the script via `CodeActRunner` (Decision A) under the CodeAct SandboxPolicy; the namespace exposes **only** the permission-proxy shims. |
| `format_feedback` | stdout / return value / tracebacks → LLM. |

**Per-call gate (the real boundary, unchanged from owner-signed model):** each in-code
`tool()` call IPCs to the parent → enters the **same OS pre-execute pipeline** PR-1
establishes: **exclude-gate on the resolved effective name** (`_excluded_result`,
pre-dispatch) → `dispatch_tool` (permission via `permission_resolver`, Control IR, P5) →
P6 `tool_called/returned/failed` + WAL `step_*` (so act-turn rewind + audit are unchanged).
A CodeAct call is gated **≥** a JSON call: same exclude + permission gate **+** sandbox
containment.

---

## 5. Existing-mechanism correspondence (owner sign-off material — settled)

| CodeAct element | Existing mechanism REUSED | New? |
|---|---|---|
| Sandbox isolation | `sandboxed_exec` SandboxBackend (Landlock/Seatbelt/Noop), `#1326` operator-policy-wins | reuse |
| Code execution | `python_runner` + `_python_harness` (timeout, crash-iso, restricted `__builtins__`/allowlist) | reuse + inject shims |
| Permission gate | `dispatch_tool` → `permission_resolver.require_*` (P5/Control IR) | reuse |
| Exclude gate (#1406/#187) | PR-1 OS pre-execute exclude gate (`_excluded_result`) — proxy calls re-enter it | reuse |
| Fail-closed | Landlock/Seatbelt availability gate (no unsandboxed CodeAct fallback) | reuse |
| Audit / time-travel | per-proxied-call P6 + WAL `step_*` via parent `dispatch_tool` | reuse |
| **Permission-proxy IPC callback** | — (harness is one-shot today) | **NEW (the only one)** |

**SandboxPolicy defaults** for CodeAct: `network=False`, `allow_subprocess=False`,
`write_paths`=workspace, `timeout` — the safe baseline, **operator-overridable** via the
existing `sandbox.policy` mechanism (configured policy wins, same as `sandboxed_exec`). No
new policy surface. **Fail-closed**: CodeAct *unavailable* (not degraded-to-unsafe) when
the sandbox is unavailable.

---

## 6. Build staging (post-design-review; WIP-push at each step)

1. **S1 — 3-arm match (single-owned seam, decision B).** Generalize `_run_scheme_tool_round`
   to `match interp: Execute | CodeBlock | RePresent`. Execute arm byte-identical; RePresent
   arm = e2e-agreed stub; CodeBlock arm wired to `CodeActScheme.execute`. *Tier-2 invariant:
   Execute path unchanged (the 22-test regression + golden-shape stay green).*
2. **S2 — CodeActRunner (decision A, the new mechanism).** socketpair + `pass_fds` spawn +
   concurrent `_service_control_socket` → parent exclude+`dispatch_tool` per request.
   Reuse SandboxPolicy/profile/harness-namespace. *Tier-2: proxied call re-enters the OS
   gate (exclude + permission), per-call, pre-dispatch.*
3. **S3 — CodeActScheme (4 methods).** build_presentation code-API (excluded omitted) /
   interpret CodeBlock / execute via CodeActRunner / format_feedback. Register via
   `register_scheme` (P7 polymorphic; no OS name-branching).
4. **S4 — config + docs.** `reyn.yaml` scheme selector + SandboxPolicy override doc;
   `control-ir.md` sync if a new op kind appears (none expected — reuses `dispatch_tool`).

**Test posture (per testing.ja.md):** Tier-2 OS-invariant (Execute byte-identical;
proxied-call gate re-entry; fail-closed when sandbox unavailable) + Tier-3 LLMReplay for
the CodeAct round shape. **No mocks** — real SandboxBackend (skipif on platform for the
real-sandbox path, honest-scope per the optional-dep/real-env discipline), real
`dispatch_tool` + `permission_resolver`. The duplex IPC is verified with a real socketpair
+ real harness child (no fake channel).

---

## 7. Design-review asks (lead + e2e)

- **Q1 (lead):** dedicated `CodeActRunner` (duplex socketpair + concurrent service) over
  extending `SandboxBackend.run` — confirm (§2).
- **Q2 (e2e): AGREED** (broker 04:54) — 3-arm match + `RePresent` unreached-assert stub in
  PR-3; PR-4 owns the `interp.refinement.candidates` contract + bounded-loop body (§3).
- **Q3 (lead):** SandboxPolicy default/override + fail-closed posture confirmed as settled
  (§5) — re-confirm no change from the owner sign-off.

No build code until Q1/Q2 clear. WIP-pushed for review.
