# ADR-0018: Cross-agent discard chain notification

**Status**: Accepted (2026-05-04)
**Track**: R-D14 (commit `d13bf6f`)

## Context

`/skill discard <id>` aborts a skill_run mid-flight, calling
`SkillRegistry.complete(status="discarded")`, dropping pending
interventions, and cancelling the asyncio task.

When the discarded skill_run is the processing target of a multi-agent
delegate chain (agent A → delegate → agent B → spawn skill_run
B-456), agent A's `pending_chain` was unaware of B's discard. A had to
wait for the `chain_timeout_seconds` watchdog to fire before learning
the chain was effectively dead.

Default `chain_timeout_seconds` is 60 s, but production deployments
configure it for realistic delegation durations:

| Delegation example | Realistic time | Configured timeout |
|---|---|---|
| Short Q&A | 10 – 30 s | 60 s |
| Notion research | 1 – 3 min | 5 min |
| Multi-file refactor proposal | 5 – 15 min | 30 min |
| Long article: outline → draft → review | 30 – 60 min | 1 – 2 h |

For long-timeout configurations, the watchdog wait is unacceptable.
Operator presses `/skill discard B-456` expecting fast feedback; A
hangs for an hour. R-D14 closes that gap.

Out-of-scope clarifications during design (user direction):

- **Cross-process delegation**: Reyn intentionally does not target
  multi-process inside one workspace. Cross-process / cross-Reyn
  communication is the future A2A protocol's job. R-D14 only handles
  in-process multi-agent delegation. WAL durability across processes
  is therefore not a concern here.
- **Cascade through nested skills**: a B-side skill that has further
  delegated to C is not handled by R-D14 (= "discard B notifies A,
  but doesn't propagate through B's own pending chains to C"). The
  cascade is tracked as future PR-discard-cascade-reissue.

## Considered alternatives

- **A. Polling**: A periodically checks B's skill registry. Wastes
  cycles; lag still depends on poll interval.
- **B. Reduce `chain_timeout_seconds` defaults.** Trades one config
  problem for another; long delegations break.
- **C. Direct cross-agent notification on discard.** When B discards a
  skill_run that was processing a chain, look up the chain's waiter
  (= A) and force-resolve A's pending_chain with a "peer discarded"
  reason.

## Decision

**Adopt C.** Five-layer architecture:

1. **`ChatSession.running_skills_chain: dict[str, str | None]`** —
   maps run_id to the chain_id (if any) that the run is processing.
   Populated in every spawn path (`_spawn_skill`, `_run_one_skill`,
   `_spawn_resumed_skill`); cleaned up in `_cleanup` callback.
2. **`ChainManager.find_chain(chain_id) -> _PendingChain | None`** —
   read-only public API on the chain manager (replacing the previous
   pattern of poking `self._chains` directly).
3. **`AgentRegistry.notify_chain_discarded(chain_id, by_agent_name,
   reason="peer_discarded")`** — scans all sessions, finds the
   waiter via `ChainManager.find_chain`, calls the waiter's
   `_on_chain_peer_discarded` handler. Returns whether a waiter was
   found.
4. **`ChatSession._on_chain_peer_discarded(chain_id, peer, reason)`**
   — mirror of `_on_chain_timeout_fire`. Force-resolves the pending
   chain, emits a `chain_peer_discarded` event, sends an "agent X
   discarded its skill_run" agent_response upstream to the chain's
   `origin_agent`.
5. **`/skill discard` slash wiring**: after the existing cancel + drop
   + complete sequence, call `registry.notify_chain_discarded(...)`
   if the run had a chain_id.

The five layers are deliberate: the `running_skills_chain` map and
the `find_chain` API are reusable for future cross-agent
introspection (the cascade work will need both).

## Consequences

**Positive:**

- A's wait collapses from minutes-to-hours to milliseconds.
- A receives an explicit "peer discarded" message, more informative
  than a generic timeout.
- The new `chain_peer_discarded` event is a first-class audit event,
  separate from `chain_timeout_fired`, distinguishing operator-driven
  vs. environmental causes.
- `running_skills_chain` and `find_chain` are usable by the future
  cascade-reissue PR without further plumbing.

**Negative:**

- One more cross-session API on AgentRegistry, expanding its
  orchestration surface. Justified by the symmetry with
  `restore_all` and `notify_chain_timeout` (which already cross
  sessions).
- A waiter further upstream (A → A' → B chain) still relies on the
  per-hop notification path; without the cascade work, only the
  immediate parent of the discarded run is notified. Documented as
  R-D14 narrow scope; multi-hop cascade is the cascade-reissue PR.

**Precluded:**

- Process-boundary discard notifications. Out of scope as documented;
  A2A protocol is the future surface.

## References

- Commit `d13bf6f` — implementation + Tier 2 / 3 tests
  (`test_chain_peer_discarded_notify.py`,
  `test_chain_manager_find_chain.py`)
- [ADR-0017](0017-parent-run-id-nested-skill-path.md) —
  `parent_run_id` field used by the planned cascade work
- discussion-log Phase 13
