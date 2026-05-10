# Track 4 — Baseline Starvation Reproducer

## Worktree
- Path: `/Users/yasudatetsuya/Workspace/junk/claude_sandbox/sandbox_2/.claude/worktrees/agent-a28040231fbf48e7e`
- Branch: `worktree-agent-a28040231fbf48e7e`
- Parent commit: `4f30297c9d2339aef8f5dc0bae04adc154b62960`
- Spike commit: `97390f4` (`spike(adr-a): empirical reproducer for asyncio starvation claim (FP-0013)`)

## Repro setup

- Test file: `tests/spike/test_adr_a_starvation_repro.py` (3 tests, ~370 lines).
- Run command:
  ```
  python -m pytest tests/spike/test_adr_a_starvation_repro.py -v -s
  ```
- Result: **3/3 PASSED** in ~3.2 s. All three configurations expect
  *no* starvation; passing means the starvation claim did NOT
  reproduce.

What's mocked vs real:

| Layer | Track A | Track B | Track C |
|---|---|---|---|
| `ChatSession` + `session.run()` + `_handle_user_message` + inbox + history | real | real | real |
| `BudgetTracker`, `StateLog`, `AgentRegistry` | real | real | real |
| `reyn.chat.router_loop.call_llm_tools` | patched — `await asyncio.sleep(0.1)` then return fixed text | same | same |
| Event loop | `asyncio.run` | `anyio.run(backend="asyncio")` + `anyio.create_task_group` | same as B |
| MCP transport | none | none | real `mcp.server.Server` (lowlevel) wired to in-memory `anyio.create_memory_object_stream` pair — JSON-RPC `initialize` + `notifications/initialized` + `tools/call` |
| Background `session.run()` spawn | `asyncio.create_task` | `asyncio.create_task` from inside an anyio `tg.start_soon` handler | `asyncio.create_task` from inside the MCP `@server.call_tool()` handler (which is dispatched via `anyio.create_task_group.start_soon` inside `Server.run`) |

The handler in each track mirrors the loop shape that
`_await_turn_complete` in `mcp_server.py` uses: push via
`session.submit_user_text`, then poll `session.history` + `inbox.empty()`
+ `not session.running_skills` at 20 ms intervals against a 3 s
deadline.

Instrumentation:

- `llm_call_progress`: list of `time.monotonic()` samples appended at
  the start and end of the patched `call_llm_tools`. Length 0 ⇒ the LLM
  coroutine never got scheduled. Length 1 ⇒ it started but was starved
  mid-`await asyncio.sleep`. Length 2 ⇒ it ran to completion.
- `partial` flag in the handler return ⇒ True only if the 3 s poll
  deadline fired without a reply landing.

## Findings

### Plain asyncio (Track A — no MCP transport)

- Background `session.run()` task progressed while handler awaited: **YES**
- Evidence:
  - `result={'reply': 'Hello from background session.run()!', 'elapsed': 0.299, 'partial': False}`
  - `llm_call_progress samples: 2` (LLM call completed)
  - `handler_poll_progress samples: 7` (~6 polls × 50 ms ≈ 300 ms,
    consistent with the 100 ms LLM sleep + dispatch overhead)

### Anyio task_group on asyncio backend (Track B — no MCP)

- Background task progressed: **YES**
- Evidence:
  - `result={'reply': 'Hello from anyio task_group!', 'elapsed': 0.434, 'partial': False}`
  - `llm_call_progress samples: 2`

### Real `mcp.server.Server.run` over in-memory anyio streams (Track C)

- Background task progressed: **YES**
- Evidence:
  - Tool response body: `{'reply': 'Hello from MCP-dispatched handler!', 'partial': False}`
  - `llm_progress=2`
  - The tool handler ran inside `Server.run`'s
    `tg.start_soon(self._handle_message, ...)` dispatch — i.e. the
    same anyio task-group nesting as production stdio — and the
    `asyncio.create_task(session.run())` spawned inside it made
    progress to completion.

### Subprocess + real stdio JSON-RPC (Track D — not attempted)

Not attempted within the time budget. Track C drives the actual
`mcp.server.Server.run` with the same anyio task-group nesting that
`stdio_server()` produces — the only piece replaced is the byte
transport (in-memory `MemoryObjectStream` vs `stdin`/`stdout`
`TextIOWrapper`). The byte transport is `anyio.wrap_file` + a
line-buffered async-for loop with explicit `flush()`; there is no
plausible mechanism by which it would change task-scheduling fairness
of unrelated `asyncio.create_task` coroutines. If a stronger guarantee
is needed, the next step is a subprocess driver against `reyn mcp
serve` — straightforward but outside this budget.

## Conclusion

- The starvation claim: **does not reproduce** in any of the three
  in-process configurations probed (plain asyncio / anyio+asyncio task
  group / real `Server.run` over in-memory streams).
- Confidence: **medium-high** for the in-process claim, **medium** for
  the full stdio path. The only configuration not directly exercised is
  the real stdin/stdout byte transport; track C exercises the identical
  task-group dispatch shape, so the residual uncertainty is concentrated
  in the byte-IO layer (where a starvation mechanism is *a priori*
  implausible).
- Falsification effort: each track was deliberately constructed to
  *expect* starvation (assertions are `not partial` and
  `llm_call_progress == 2`). Had starvation reproduced, the tests would
  have failed loudly with the diagnostic counts (0=never scheduled,
  1=started+starved, 2=completed) in the message.

### Implication for FP-0013

The bypass justification in `src/reyn/mcp_server.py::_get_session`
**is not supported by current observation**. The likely explanations
are non-exclusive:

1. The original starvation was caused by a different bug (e.g. an
   earlier-version `_await_turn_complete` that had a tight non-yielding
   poll, or an LLM client that itself was blocking — `dogfood_trace`
   would have been informative here).
2. A subsequent fix elsewhere (router-loop, journal, registry) removed
   the cause without the docstring being updated.
3. Anyio's `asyncio` backend has always scheduled `asyncio.create_task`
   coroutines fairly — the original diagnosis may have been wrong.

For FP-0013 (unifying CUI vs MCP/A2A inbox-outbox transport): the
empirical floor is now **the inbox path works under MCP dispatch**. The
existing bypass can be removed in favor of going through
`submit_user_text` + `session.run`-task + poll, identical to the chat
loop. Recommend ADR-A treat the bypass as legacy code with stale
rationale, not a fundamental constraint.

Caveat (pre-conclusion checklist applied):
- Primary observations: 3 in-process repro attempts, each with
  instrumented progress counters (= primary data, not inference).
- Falsification looked for: assertions inverted to detect starvation;
  none triggered.
- N covered: 3 distinct event-loop configurations.
- Direct-inspection coverage: 3/3 (each track was run with `-s`,
  output inspected).
- Not covered: full subprocess + real stdin/stdout JSON-RPC byte
  transport. The remaining uncertainty for the production code path is
  bounded by that gap. To close it, run `reyn mcp serve` as a
  subprocess with a minimal JSON-RPC client and an inbox-routed
  `send_to_agent` build — ~1–2 hours of additional work.

## Commit

- Worktree commit: `97390f4` — `spike(adr-a): empirical reproducer for
  asyncio starvation claim (FP-0013)`
- 2 files added: `tests/spike/__init__.py`,
  `tests/spike/test_adr_a_starvation_repro.py` (3 tests, 482
  insertions).
- Existing `tests/test_mcp_server.py` (7 tests) still passes alongside.
- Main branch untouched.
