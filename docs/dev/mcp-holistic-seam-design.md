# MCP holistic seam — design assessment (defeat-all-hypotheses)

**Status:** design-first / pre-implementation. **GO-gated** (owner approval before build).
**Basis:** current code on `origin/main` (per lead — verify with `git show origin/main:<path>`).
**Author:** e2e-coder. **Motivation:** owner — "MCP 修正を繰り返してる、モグラ叩きにならない
綺麗な直し方を再検討して" (we keep patching MCP per-path; find a clean, non-whack-a-mole fix).

## 1. Why the whack-a-mole happens (root cause)

Fault-isolation + lifecycle + timeout were each applied **per entry path**, not at a shared seam:

- **P2 (#2406)** put a task-affine pool + a contain-all boundary on the **op-call** path only.
- **S3 (#…)** put per-call timeout on the op-call path only.
- The **list** and **probe** paths kept `async with MCPClient(...)` + `except Exception` — which
  does NOT catch a bare `BaseExceptionGroup` (a cancel-mixed group is not an `Exception` subclass).
  → the list-path crash recurred (owner's live crash) because the completeness sweep missed it.

Each new path (or a future one) can re-introduce a crash/leak because the guarantees live in the
callers, not in one place. **Fix class = a single seam every MCP op flows through.**

## 2. Current MCP entry paths (inventory)

| # | Path | Site | Lifecycle | Fault boundary | Timeout |
|---|------|------|-----------|----------------|---------|
| 1 | op-call | `_mcp_call_tool` (session.py) → op_runtime/mcp.py `_execute` → `MCPClientPool.get` + `client.call_tool` | pool (task-affine) ✓ | `except BaseException` + `is_or_contains_control_flow` re-raise + `describe_fault` contain ✓ | per-call ✓ |
| 2 | list | `_mcp_list_tools` (session.py) → `async with MCPClient(cfg) as c: c.list_tools()` | none ✗ | `except MCPError / except Exception` ✗ (misses bare BEG) | none ✗ |
| 3 | probe-tools | `_probe_server_tools` (interfaces/cli/commands/mcp.py) → `async with asyncio.timeout(): async with MCPClient(cfg) as c: c.list_tools()` | none ✗ | `except Exception` ✗ | wall-clock only |
| 4 | probe-status | `_probe_status` (interfaces/cli/commands/mcp.py) → `async with MCPClient(cfg):` (init handshake) | none ✗ | `except Exception` ✗ | none |
| 5 | adapter probe | `router_host_adapter.py` `_probe_all` → calls #3 and the `_mcp_list_tools` cb (#2) | (delegates) | (delegates) | (delegates) |
| 6 | direct/other | any `MCPClient(...)` construction | — | — | — |

**Path 1 is the reference implementation** — it already has exactly the boundary lead wants. The
seam **generalizes path 1's proven boundary + pool + timeout to a shared gateway** that paths 2–6
route through. Paths 2–4 become thin callers; #5 delegates; #6 is eliminated (grep-gated).

## 3. Proposed seam: `MCPGateway`

One object owns all MCPClient contact. It exposes exactly the operations the system needs:

```python
class MCPGateway:
    """The single seam for all MCP I/O. Every MCP op runs here so lifecycle,
    fault-isolation, timeout, and offload are enforced in ONE place."""

    async def list_tools(self, server, cfg, *, agent_id=None) -> list[dict]: ...
    async def call_tool(self, server, tool, args, cfg, *, agent_id=None,
                        progress_cb=None, timeout=None) -> dict: ...
    async def probe(self, server, cfg, *, agent_id=None) -> str: ...   # initialize-only status
```

Every op body has the SAME skeleton (the four guarantees, in one place):

```python
async with MCPClientPool() as pool:                       # [3] task-affine lifecycle
    try:
        client = await pool.get(server, cfg, agent_id=agent_id)   # open in the pool's task
        result = await _with_timeout(client.<op>(...), timeout)   # [4] per-call timeout
        return _ok_shape(result)                                  # + offload marker (transient)
    except BaseException as exc:                                  # [2] contain-all
        if _is_real_control_flow(exc):
            raise
        return _error_shape(describe_fault(exc))
    # pool.__aexit__ closes the client IN THIS TASK → [1] joins SDK sub-tasks, contains teardown
```

### The four guarantees

- **[1] structured sub-task join** — `MCPClient.close`/`__aexit__` (invoked by `pool.__aexit__`
  in the owning task) **awaits the SDK's internal task group to completion within scope**, and its
  own teardown runs inside the same contain-all boundary. An SDK sub-task fault
  (`stdout_reader`/`receive_loop` on subprocess death) surfaces at the awaited close **in-scope** —
  it can never orphan to loop-level ("Task exception was never retrieved"). **Defeats hypothesis (b).**
- **[2] contain-all boundary + real-control-flow predicate** — `except BaseException`; re-raise ONLY
  genuine control flow, contain everything else → error result. Predicate:
  ```python
  def _is_real_control_flow(exc) -> bool:
      if isinstance(exc, (KeyboardInterrupt, SystemExit)):
          return True
      t = asyncio.current_task()
      real_cancel = t is not None and t.cancelling() > 0        # our task is genuinely cancelled
      if isinstance(exc, asyncio.CancelledError):
          return real_cancel
      if isinstance(exc, BaseExceptionGroup):
          cf, _rest = exc.split((KeyboardInterrupt, SystemExit, asyncio.CancelledError))
          return cf is not None and (real_cancel or _has_ki_or_se(cf))
      return False
  ```
  A **spurious** internal cancel (anyio folding a faulted sibling, `cancelling()==0`) is **contained**,
  not re-raised. **Defeats (a1) cancel-mixed BEG, (a2) all-Exception ExceptionGroup, (c) plain
  transport** — exception-structure-independent.
- **[3] pool task-affinity** — reuse `MCPClientPool` (a359 P2): open+close on one task, no
  cross-SDK-task teardown.
- **[4] per-call timeout** — reuse S3's `call_timeout_seconds` resolution (generous default,
  per-server override, `0`=opt-out).

### Offload integration (connects #2396)
`list_tools`/`call_tool` results are **transient** (no on-disk source) → they route through the
shared `decide_payload_field` offload marker, consistently. (file_read is disk-backed → truncate,
NOT offload — see PR #2417; different axis, no conflict.)

## 4. Migration — thin callers + completeness gate

- `_mcp_list_tools` → `return await gateway.list_tools(...)` (errors already shaped by the seam).
- `_mcp_call_tool` / op-handler → `gateway.call_tool(...)` (the op handler's current boundary is
  absorbed into the seam; no duplicate).
- `_probe_server_tools` → `gateway.list_tools(...)`; `_probe_status` → `gateway.probe(...)`.
- Pending **list-path crash containment is absorbed here** — no separate patch.
- **Completeness gate (structural test):** grep asserts NO `MCPClient(` / `async with MCPClient`
  outside `reyn/mcp/gateway.py` (+ the pool). A new path physically cannot bypass the seam.
- The a359-DIAG temporary block removal folds into this work.

## 5. Acceptance matrix (traceback-independent; no live crash-repro needed)

Inject each fault form at `{initialize, list_tools, call_tool, teardown}`, across the list / call /
probe seam entrypoints; assert the seam **contains** (reyn survives + error result), except real
control flow which must **propagate**:

| form | injected fault | expected |
|------|----------------|----------|
| a1 | cancel-mixed bare `BaseExceptionGroup` `[BrokenResource, CancelledError]`, `cancelling()==0` | **contained** → error result (spurious cancel stripped) |
| a2 | all-`Exception` `ExceptionGroup` `[ConnErr, RuntimeError]` | **contained** |
| b  | SDK sub-task raises **off-task** after return | [1] join → surfaces in-scope → **contained** (no "never retrieved") |
| c  | plain transport `Exception` (`BrokenResourceError`/`ConnectionReset`) | **contained** |
| d  | real control flow: `KeyboardInterrupt` / `SystemExit` / `CancelledError` with `cancelling()>0` | **re-raised** (propagates) |

Plus a **structural** test (grep) that every MCP path routes through the gateway. Owner does a final
Windows yes/no ("still crash?") — no paste needed. Injection harness already prototyped (my a359
repro scripts).

## 6. Open questions for lead / owner

1. **Gateway module vs extend pool** — lean: a thin new `reyn/mcp/gateway.py` wrapping
   `MCPClientPool` (pool stays the lifecycle primitive; gateway adds boundary+timeout+offload).
2. **Pool sharing** — list/probe are one-shot (own pool per call is fine); op-call reuses a per-turn
   pool. Gateway accepts an optional injected pool (reuse) or creates its own (one-shot).
3. **Confirm** MCP call/list results should offload as transient (yes per owner; only file_read is
   disk-backed/exempt).
4. **Build sequencing** — land the gateway + move paths 2–4 in one PR (with the completeness grep +
   acceptance matrix), then a follow-up removes the a359-DIAG block. Or split the DIAG removal in.

## 7. Why this is the non-whack-a-mole fix

The guarantees stop living in N callers and live once, at a seam every op must pass. Completeness is
structural (grep-enforced), not a manual sweep. A new MCP entry path is a thin caller by
construction and inherits containment — it **cannot** re-introduce the crash class. The exact
failing exception structure stops mattering: [2] contains anything that isn't real control flow, and
[1] removes the only escape route (off-task orphaning).
