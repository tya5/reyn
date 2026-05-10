# Track 5 — Pumping Prototype

## Worktree
- Path: `/Users/yasudatetsuya/Workspace/junk/claude_sandbox/sandbox_2/.claude/worktrees/agent-ab34f575194930db2`
- Branch: `worktree-agent-ab34f575194930db2`
- Commit: `05dc2368dd76f9e29508997c9781b9d878b5e4e3`
- Base: `4f30297` (main HEAD at spike start)

## Implementation summary

### `ChatSession.run_one_iteration`

`src/reyn/chat/session.py:907` — extracted from the body of `run()`'s
`while True:` block. Single dispatch step over the inbox: returns
`True` to continue, `False` on shutdown. `run()` itself collapses to
`while await self.run_one_iteration(): pass` so the existing chat
loop semantics (chat_started/chat_stopped, _drain_on_shutdown, final
`__end__` outbox push) are preserved unchanged.

Key snippet:

```python
async def run_one_iteration(self) -> bool:
    kind, payload = await self._consume_inbox()
    if kind == "shutdown":
        return False
    if kind == "user":
        await self._handle_user_message(
            payload.get("text", ""),
            chain_id=payload.get("chain_id") or _new_chain_id(),
        )
    elif kind == "agent_request":
        await self._handle_agent_request(payload)
    elif kind == "agent_response":
        await self._handle_agent_response(payload)
    return True
```

### `send_to_agent_impl_pumping`

`src/reyn/mcp_server.py:243` — sibling to the existing
`send_to_agent_impl` bypass. Instead of inline-calling
`session._handle_user_message`, it:

1. Calls `await session._put_inbox("user", {"text": message, "chain_id": chain_id})`.
2. Loops `await session.run_one_iteration()` (each call wrapped with
   `asyncio.wait_for(..., timeout=remaining)`) until either a reply
   for this `chain_id` has landed in history AND the session is idle
   (`inbox.empty() and not running_skills`), or the per-call timeout
   fires.
3. Applies the same 50ms `_IDLE_GRACE_SECONDS` window as the bypass
   path to absorb a possible follow-up `agent_response`.
4. Awaits any active `running_plans` tasks (parity with bypass — keeps
   the plan-mode async dispatch behaviour comparable).
5. Harvests new `role="agent"` history entries scoped to `chain_id`.

Reuses the bypass's `_AGENT_LOCKS` for the same per-agent serialization
guarantee.

### Lines changed
- `src/reyn/chat/session.py`: +24 / -13 (mostly cosmetic — extracted
  body, `run()` now a one-liner loop)
- `src/reyn/mcp_server.py`: +110 / 0 (new function added; existing
  bypass left untouched)
- `tests/spike/test_adr_a_pumping_prototype.py`: +210 (4 tests)
- `tests/spike/__init__.py`: new empty file

## Test results

- Test file: `tests/spike/test_adr_a_pumping_prototype.py`
- Run command:
  `python -m pytest tests/spike/test_adr_a_pumping_prototype.py -v -s`
- Result: **4 passed in 4.30s**

Outcomes:

| Test | Result | Timing |
|---|---|---|
| `test_run_one_iteration_dispatches_user` | PASS | ~0.1s |
| `test_run_one_iteration_returns_false_on_shutdown` | PASS | <0.1s |
| `test_pumping_round_trip` (LLM stub `sleep(0.1)`) | PASS | ~0.3s wall |
| `test_pumping_matches_bypass` | PASS, replies identical | bypass=158.5ms, pump=264.4ms |

Regression: `pytest tests/test_mcp_server.py -q` → **7 passed**.
Broader `tests/ -k "session or chat or mcp or a2a"` (excluding the
pre-existing env-dependent `test_replay_skill_router.py` failure):
**334 passed**.

## Starvation verdict

**No starvation observed under plain asyncio.**

Evidence (primary, from the test run above):

1. `test_pumping_round_trip` — the LLM stub `await asyncio.sleep(0.1)`
   completes inside the pumped `run_one_iteration` call, the reply
   text reaches `session.history`, and `send_to_agent_impl_pumping`
   returns `partial=False` with `"Pumped reply!"` in the reply
   field. Total wall time well under 2s. If the LLM call were
   starving (the failure mode that motivated the MCP/A2A bypass)
   we would see `partial=True` with the "still working" placeholder.
2. `test_pumping_matches_bypass` — both transport paths produce the
   exact same reply text for the same prompt + same stubbed LLM.
   Pumping is ~100ms slower (264ms vs 158ms) — that delta is the
   inbox WAL journal write + the 50ms idle grace, both of which are
   acceptable for a request handler.
3. The 4 spike tests + 7 MCP server tests + 334 chat/session/mcp/a2a
   tests all pass without modifying the existing bypass path, so
   the `run()` decomposition is behaviour-preserving.

### Caveats / scope limits

- **Plain asyncio only.** The historical starvation reproduces only
  under the MCP SDK's stdio anyio transport (per the `_get_session`
  docstring). This spike runs under direct `asyncio.run(...)`, not
  through `stdio_server()`. Strictly speaking the spike rules out
  asyncio-level starvation but does NOT independently verify
  anyio/stdio behaviour — that needs an MCP-stdio integration test
  before FP-0013 lands.
- **Single-turn only.** The skill_completed stress test (FP-0012 +
  pumping) was not attempted in this spike — running_skills + inbox
  re-entry across multiple iterations is a worthwhile follow-up but
  the basic round-trip is the load-bearing claim for ADR-A.
- **LLM is stubbed.** Real-LLM behaviour (network latency, partial
  streaming) is not exercised. The 100ms sleep is a stand-in.
- **Concurrency under load not measured.** Just one concurrent
  request per agent (the existing `_AGENT_LOCKS` already serializes).

## Recommendation for FP-0013

**Green-light — proceed as proposed, with two clarifications.**

### Why green-light

- The pumping model works empirically (4/4 spike tests, 334/334
  regression tests).
- The decomposition is mechanical and small (~24 net lines in
  `session.py`).
- Bypass and pumping produce byte-identical replies — there is no
  semantic divergence to migrate around.
- Per-agent serialization (`_AGENT_LOCKS`) keeps the existing
  concurrency contract; no new races introduced.

### Specific concerns / adjustments

1. **anyio/stdio verification still required.** Before deleting the
   bypass, run a real MCP stdio session test (Claude Code spawns
   `reyn mcp serve`, sends `send_to_agent`) end-to-end. The whole
   reason the bypass exists is anyio scheduling; the spike's plain
   `asyncio.run` can't speak to that. Suggest a Track 6 follow-up.
2. **Idle-grace window.** The current 50ms grace is borrowed from
   the bypass. Under pumping it is slightly redundant (the loop
   itself already pumps `agent_response` follow-ups in the same
   iteration). Worth re-tuning down to 0–20ms in the real migration
   to reclaim some of the ~100ms overhead.
3. **`_put_inbox` durably journals** (WAL write) — that's the bulk
   of the 100ms overhead. The chat path always pays this cost; the
   MCP/A2A bypass currently does not. Migrating means MCP/A2A turns
   become durable too — a feature, but call it out.

### Cost adjustment vs FP-0013's LARGE estimate

The proposal doesn't exist in the repo yet (no `docs/deep-dives/proposals/0013-...`
file at spike time), so I can't quote its current estimate. From the
delta of this spike: the OS-side changes for ADR-A are ~25 net lines
in `session.py` + ~80 lines in `mcp_server.py` (replace bypass with
pumping). The A2A FastAPI handler (`reyn web`) will mirror that.
Hypothesis: **the migration itself is SMALL-MEDIUM, not LARGE** —
LARGE is probably anchored to the anyio/stdio verification + soak
testing rather than the code change. Recommend re-scoping the
estimate around the verification work, not the implementation.

## Commit

`05dc2368` — `spike(fp-0013): pumping prototype — run_one_iteration + send_to_agent_impl_pumping`

4 files changed, 344 insertions(+), 13 deletions(-):
- `src/reyn/chat/session.py` — extracted `run_one_iteration`
- `src/reyn/mcp_server.py` — added `send_to_agent_impl_pumping` (sibling)
- `tests/spike/__init__.py` — new
- `tests/spike/test_adr_a_pumping_prototype.py` — 4 spike tests
