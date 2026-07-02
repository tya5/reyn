# a359 — MCP task-affinity crash: Windows verification (P3)

**Status: TEMPORARY diagnostic phase.** This doc + the `reyn.mcp.a359diag` logging in
`src/reyn/mcp/pool.py` exist ONLY to confirm, on the owner's Windows environment, that the
`BaseExceptionGroup` / `BrokenResourceError` / `ConnectionReset` crash on `list_mcp_tools` (and MCP
tool calls) is gone after the structured-client fix (P1 #2405 + P2 #2406). **Once confirmed, a
follow-up PR removes this doc + the `a359-DIAG` logging block** (owner asked for its removal on
resolution).

## Why Windows-only
The root cause (a359) is `MCPClient`'s deferred `AsyncExitStack` closing the SDK `stdio_client` /
`ClientSession` internal anyio task-group scopes **cross-task**. On Unix (`SelectorEventLoop`) the
cross-task teardown is *tolerated* — every repro scenario SURVIVES, so the crash **cannot be
RED-verified on Unix**. It manifests only on the Windows `ProactorEventLoop` (subprocess/job-object
teardown ordering). Hence the fix's *sufficiency* must be shown on Windows.

## Procedure 1 — the repro harness (fastest signal)
On Windows, from the repo root, on the fixed `main`:

```
python scripts/mcp_stdio_repro.py
```

It prints the platform + event loop (expect `platform=win32 loop=ProactorEventLoop`), then runs each
lifecycle scenario and reports `SURVIVED` or `CRASHED` + any owner-keyword match
(`BaseExceptionGroup` / `cancel scope` / `BrokenResource` / `ConnectionReset` / ...).

- **Expected on the FIXED code:** every scenario `SURVIVED`, no owner-keyword match — including
  `cross_task_close` and `no_close_gc_teardown` (the ones most likely to CRASH on Proactor with the
  pre-fix lifecycle).
- **To see the contrast** (optional): run the same harness on a pre-fix commit (before #2405) — the
  `cross_task_close` / `no_close_gc_teardown` arms should `CRASHED` with an owner-keyword match on
  Proactor. That contrast is the RED→GREEN evidence Unix can't provide.

## Procedure 2 — the real crash path (`list_mcp_tools`) with diagnostic logging
Reproduce the owner's original crash path with an MCP server configured, capturing the temporary
`reyn.mcp.a359diag` logger at INFO (add to logging config, or set the logger level):

```python
import logging
logging.getLogger("reyn.mcp.a359diag").setLevel(logging.INFO)
```

Then run the interactive `list_mcp_tools` (and a tool call). The diagnostic emits, per client:

```
a359-diag: opened MCP client server=<s> open_task=<T>
a359-diag: closed MCP client server=<s> open_task=<T> close_task=<T> outcome=ok
```

- **Expected on the FIXED code:** `open_task == close_task` and `outcome=ok` for every client, and
  **no** `BaseExceptionGroup` / `BrokenResourceError` crash surfaces to the user.
- If a transport fault occurs, the line reads `teardown-fault=<repr>` and it is **contained** (logged,
  not crashing the loop) — and an MCP tool call fault surfaces to the LLM as a contained error result
  (owner req), not a crash.

## After confirmation
Post the harness output + the `a359-diag` lines on the arc issue. Then the follow-up PR removes:
- the `a359-DIAG` block + the `_diag_log` / `_open_tasks` / `_task_name` diagnostics in
  `src/reyn/mcp/pool.py`, and
- this doc.

The structured pool + fault-isolation (P1/P2) stay — only the temporary instrumentation is removed.
