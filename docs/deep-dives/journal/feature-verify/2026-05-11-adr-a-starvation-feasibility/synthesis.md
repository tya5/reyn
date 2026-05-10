# ADR-A Starvation Feasibility — Synthesis

**Date**: 2026-05-11
**Trigger**: FP-0013 §Open design questions ADR-A — verify empirically whether
pumping `session.run_one_iteration()` from the MCP/A2A request handler task
avoids the asyncio starvation that motivated the bypass.
**Method**: 5-track parallel sonnet investigation
- 3 info-gathering tracks (= code archaeology / SDK internals / industry audit)
- 2 worktree spike tracks (= baseline starvation repro / pumping prototype)

## TL;DR — Verdict

**GREEN LIGHT FP-0013 implementation.** Pumping the inbox loop from the
request-handler task is mechanically sound, industry-standard (LangGraph
`astream`, Strawberry GraphQL subscriptions are direct precedents),
empirically validated in 3 progressively-closer harnesses (including real
`mcp.server.Server` + in-memory anyio streams + real JSON-RPC), and the
prototype passes 4/4 spike tests with ~100ms overhead vs the bypass.

**One residual verification** before deleting the bypass code: drive
`scripts/mcp_probe.py` (= real subprocess + real stdin/stdout byte
transport) against the pumping path to close the byte-transport gap
Track 4 couldn't exercise. Estimated ~1-2h follow-up.

## Per-track findings

### Track 1 — Code archaeology

The bypass was empirically observed in 2026-05-07, commit `a5678c1`. Two
non-bypass approaches were tried first and failed:

1. `4d92a78` — original: `asyncio.create_task(session.run())` + negative-signal
   poller. Empty reply.
2. `b535517` — added positive-signal (= `role="agent"` history entry required).
   Still empty reply — task itself wasn't progressing, so no history entry
   could appear.
3. `a5678c1` — abandoned the background task, drove `_handle_user_message`
   inline. **Worked first try.**

The bypass is justified by a verified differential reproducer: identical
`send_to_agent_impl` code works under `asyncio.run(...)` but fails under MCP
SDK stdio transport. `scripts/mcp_probe.py` (in tree since `a5678c1`) is the
live reproducer.

**Important caveat surfaced**: the A2A endpoint (commit `bea4d73`, ~45 min
after `a5678c1`) **inherited the bypass without independent verification**.
A2A runs under FastAPI/uvicorn (= pure asyncio, no anyio/stdio quirk). The
bypass may have always been unnecessary on the A2A side.

### Track 2 — MCP SDK + asyncio internals (mechanical analysis)

Root-cause is NOT generic scheduler unfairness. The most likely mechanism:

1. **Cancellation cascade through anyio task-group structured concurrency**.
   `Server.run()` lives inside an anyio task group; if any reader/writer
   side closes (e.g. Claude Desktop disconnect after 30s timeout), the TG
   cancels every in-flight handler. A bare `asyncio.create_task(...)`
   spawned **from inside** an anyio TG handler is NOT structurally
   contained — it's orphaned on cancel.
2. **Buffer-0 anyio memory streams** (`mcp.server.session._incoming_message_stream`)
   create rendezvous semantics. Inbox push from the request task may not
   match the background `session.run()` task's receive, especially if first-tick
   scheduling places them on the wrong side of the rendezvous.

**Prediction (= high confidence)**: pumping collapses 2 tasks → 1 task,
eliminating cross-task rendezvous AND orphan-on-cancel. Same code runs on
the same task that the SDK is already pumping; LLM awaits yield naturally
like they do today.

**Real risk for FP-0013**: the **turn-completion signal** correctness problem
that `b535517` already retired one polling approach for. `run_one_iteration`
needs a precise "this turn is done" predicate that doesn't false-positive
on inbox-empty-but-busy windows.

### Track 3 — Industry pattern audit

Pumping from the request handler is **industry standard**:

- LangGraph `graph.astream(input)` — handler iterates async generator, agent
  loop lives in the handler task
- Strawberry GraphQL subscriptions — same shape, async generator pumped by
  ASGI

AutoGen takes the opposite shape (long-lived runtime + enqueue), valid but
requires process-lifespan hooks Reyn doesn't currently have.

**Design refinements suggested**:

1. Name the pump primitive explicitly: `session.run_until_reply(reply_to)` →
   `OutboxMessage`. First-class terminating predicate (mirrors
   `graph.astream(...).__anext__()`).
2. Document `CancelledError` semantics: in-flight tool calls should complete
   their events-log writes before the handler unwinds (Strawberry hit this
   trap, issue #889).
3. For any auxiliary `asyncio.create_task` inside the pump, keep strong refs
   on `ChatSession` (Python 3.12+ GC trap, CPython #91887).
4. Defer external-job-ticket pattern (= MCP wait-tool / Bedrock AgentCore) to
   a future proposal — not needed for FP-0013's request-response shape.

### Track 4 — Baseline starvation reproducer

**Built 3 progressively-closer harnesses**:

- **A**: plain `asyncio.run` + `asyncio.create_task(session.run())`
- **B**: `anyio.run(backend="asyncio")` + `anyio.create_task_group` + nested
  handler task (mirrors `Server.run`'s nesting)
- **C**: real `mcp.server.Server` + `@server.call_tool()` handler driven via
  `anyio.create_memory_object_stream` carrying real JSON-RPC `initialize` +
  `tools/call` messages, tool handler uses background-task approach instead
  of bypass

**All 3 passed** with replies in <500ms (3s timeout). Starvation **did not
reproduce** in any of them. Instrumentation (`llm_call_progress` sampled at
start/end of faked LLM call) recorded 2 samples consistently — task started
AND completed in all harnesses.

**Gap**: real subprocess + stdin/stdout byte transport (Track D) not
attempted in time budget. Harness C exercises the identical anyio task-group
nesting `stdio_server()` produces; only the byte transport differs.

**Interpretation**: either (a) the MCP SDK has fixed the underlying issue
since 2026-05-07, or (b) the starvation requires the specific byte-transport
quirk Track 4 didn't exercise. Either way, **the bypass is not protecting
against something that reproduces under reasonably faithful in-process
harnesses today**.

### Track 5 — Pumping prototype

**Worktree**: `agent-ab34f575194930db2`, commits `05dc236` + `c062dd8`.

Implementation (= ~25 + ~80 net lines):

- `ChatSession.run_one_iteration` extracted from `run()`'s while-True body
- `send_to_agent_impl_pumping` sibling to bypass — `_put_inbox("user", ...)`
  + pump `run_one_iteration` until reply lands + idle + reply harvested

**Tests**: 4/4 pass in 4.30s
- Round-trip via pumping (LLM stub `sleep(0.1)`) → reply lands, `partial=False`,
  ~300ms wall-clock
- Byte-identical reply vs bypass (bypass=158ms, pump=264ms)
- `run_one_iteration` dispatches one user kind correctly
- `run_one_iteration` returns False on shutdown

**Regression**: 334 chat/session/mcp/a2a tests pass — `run()` collapses
cleanly to `while await self.run_one_iteration(): pass`.

**Verdict**: pumping is ~100ms slower than bypass (= inbox WAL journal + 50ms
idle grace). Acceptable; idle-grace can be retuned once pumping is the
only path.

## Cross-track synthesis

| Question | Verdict | Confidence | Evidence |
|---|---|---|---|
| Was the original starvation real? | Yes, in 2026-05-07 | High | Track 1 differential reproducer + commit msg |
| Does it still reproduce today? | Not in 3 in-process harnesses | Medium-high | Track 4 harnesses A/B/C all pass |
| Does pumping fix it mechanically? | Yes | High | Track 2 root-cause analysis |
| Is pumping the right pattern? | Yes, industry-standard | High | Track 3 LangGraph + Strawberry precedents |
| Does the pumping prototype work? | Yes | High | Track 5 4/4 tests, 334 regression green |
| Was A2A bypass ever justified? | No (inherited uncritically) | Medium | Track 1 commit timing + uvicorn != anyio/stdio |

## Implications for FP-0013

1. **Implementation strategy is sound as proposed.** No redesign needed.
2. **Open design question ADR-A: CLOSED** (= empirical green light).
3. **Cost estimate refinement**: FP-0013 estimated LARGE (~1-2 weeks). Track 5
   shows the core `run_one_iteration` decomposition + pumping wire is closer
   to SMALL-MEDIUM (~25+80 lines). The LARGE estimate is dominated by
   **verification + soak** (= real-stdio confirmation, anyio cancellation
   semantics audit, multi-agent relay migration, tactical-patch removal).
4. **Naming refinement**: Track 3 suggests `session.run_until_reply(reply_to)`
   as a first-class primitive over a generic `run_one_iteration` + caller-side
   predicate. Worth adopting.
5. **A2A migration can move first** — A2A under uvicorn never needed the
   bypass, and Track 4 found no starvation under anyio either. A2A is the
   lower-risk first migration target; MCP follows after subprocess soak.

## Residual verification (= before deleting bypass code)

1. **Subprocess + real stdio probe**. Drive `scripts/mcp_probe.py` against
   the pumping path, compare reply latency vs current HEAD bypass. Pass
   threshold: 10/10 non-empty replies within 1.5× of bypass latency.
   Estimated: ~1-2h.
2. **anyio CancelledError soak**. Disconnect mid-call (kill client during
   LLM await), verify in-flight tool calls write to events log before
   handler unwinds. Estimated: ~1-2h.
3. **Loop-liveness heartbeat**. Instrument `_receive_loop` to log a
   heartbeat each time it awaits incoming JSON-RPC; verify heartbeats
   continue during a >5s LLM call under pumping. Estimated: ~1h.

These 3 close the gaps Track 4 couldn't reach. They are NOT blockers for
**accepting** FP-0013 in principle — only for retiring the bypass code in
production.

## Recommended next step

**Accept FP-0013 in principle (= update Status from `proposed` to
`accepted`) with ADR-A closed as green-light.** Schedule the 3 residual
verifications as preconditions for the bypass-deletion commit, not for the
migration as a whole. Move the migration plan to in-progress.

## Files

- `track4_baseline_starvation.md` — Track 4 full report
- `track5_pumping_prototype.md` — Track 5 full report
- Worktree branches retained (= can cherry-pick when FP-0013 implementation
  starts):
  - `worktree-agent-a28040231fbf48e7e` (Track 4 spike tests)
  - `worktree-agent-ab34f575194930db2` (Track 5 pumping prototype)
