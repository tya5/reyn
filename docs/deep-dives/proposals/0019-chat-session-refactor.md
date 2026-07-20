# FP-0019: ChatSession Responsibility Separation — Extracting Services from session.py

**Status**: done — Wave 1+2 part 1+3 complete (CompactionController + SkillRunner + InterventionHandler + AutoResumeHandler extracted, 2026-05-13/14); Wave 2 part 2 (A2AHandler extraction, PR `a792d133`) also landed — later renamed `InterAgentMessaging` (#2188)
**Proposed**: 2026-05-11
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

`src/reyn/chat/session.py` has grown to 3,836 lines with five distinct responsibilities
mixed inside `ChatSession`: skill execution management, A2A agent protocol, intervention
routing, compaction, and auto-resume. Six service classes totaling 2,122 lines have already
been extracted into `src/reyn/chat/services/`, but the remaining logic in `session.py` is
still too dense to safely modify. This proposal completes the extraction in three waves,
reducing `session.py` to a ~600-line thin dispatcher that delegates entirely to extracted
services.

---

## Motivation

### Current state

```
src/reyn/chat/
├── session.py              3,836 lines   ← target of this FP
└── services/
    ├── budget_gateway.py     347 lines   ┐
    ├── chain_manager.py      412 lines   │
    ├── intervention_registry.py  298 lines │ already extracted
    ├── memory_service.py     389 lines   │ (2,122 lines total)
    ├── router_host_adapter.py 334 lines  │
    └── snapshot_journal.py   342 lines   ┘
```

### What remains in session.py

Five cohesive clusters are still embedded in `ChatSession`:

| Cluster | Key methods | Responsibility |
|---|---|---|
| SkillRunner | `running_skills`, `_run_stdlib_skill`, `_dispatch_routing_decision_for_user` | Skill task lifecycle (launch / track / cancel) |
| A2AHandler | `_send_to_agent`, `_send_agent_response`, `_handle_agent_request`, `_handle_agent_response`, `_resolve_pending_chain` | Agent-to-agent protocol (send / receive / chain) |
| InterventionHandler | `_maybe_answer_oldest_intervention`, `_dispatch_intervention`, `_announce_intervention`, `_wait_for_intervention_answer` | User-facing ask_user flow routing |
| CompactionController | `_maybe_compact`, `_run_compaction`, `_compaction_task` | Context compaction scheduling and execution |
| AutoResumeHandler | `_auto_resume_active_skills` | Crash recovery — restart skills from WAL on session start |

### Why this matters now

- **FP-0012 (async execution)** has LANDED (commit `c9e79d6`). Wave 1's `SkillRunner`
  extraction aligns the landed async execution model with `session.py` — the async task
  infrastructure now exists in the OS, but `session.py` is still the monolithic owner of
  the skill task dict. Extracting `SkillRunner` makes the boundary explicit.
- **FP-0011 (remove narrator)** requires changes in the routing path currently buried inside
  `_dispatch_routing_decision_for_user`. Extracting `SkillRunner` first de-risks this change.
- **Testability**: the current `session.py` cannot be meaningfully unit-tested because all
  five responsibilities share `self`. Post-extraction each service can be tested in isolation
  against its narrow API.
- **Onboarding**: contributors who want to fix an A2A protocol bug must currently read
  3,836 lines. Post-extraction the A2A surface is a self-contained ~350-line file.

### Design constraints

- Extracted services receive injected dependencies (event_log, agent_config, etc.) — no
  circular imports, no global singletons.
- `ChatSession.run()` message loop remains in `session.py` as the top-level dispatcher.
- Each service follows the same pattern as the already-extracted six: `__init__` takes typed
  dependencies, no direct `self` reference to `ChatSession` state outside its own scope.
- P6: all state mutations remain event-emitting. No behavior changes, only structural moves.
- Tests are written to Tier 1 (service contract) and Tier 2 (OS invariant) per the testing
  policy. Mocks are not used.

---

## Proposed implementation

### Wave 1 — SMALL × 2 (lowest risk, enables FP-0011; aligns session.py with landed FP-0012)

**CompactionController** (`services/compaction_controller.py`)

**LANDED** (commit `6620505`): src/reyn/chat/services/compaction_controller.py

Extracts: `_maybe_compact`, `_run_compaction`, `_compaction_task`

```python
class CompactionController:
    def __init__(self, *, llm_client, event_log, config, snapshot_journal): ...
    async def maybe_compact(self, messages: list[Message]) -> list[Message]: ...
    async def cancel(self) -> None: ...
```

Compaction is a pure background concern with no coupling to skill routing or A2A. Its
extraction removes the only background `asyncio.Task` that `ChatSession` currently owns
directly.

**SkillRunner** (`services/skill_runner.py`)

**LANDED** (commit `9ae66fa`): src/reyn/chat/services/skill_runner.py

Extracts: `running_skills` dict, `_run_stdlib_skill`, `_dispatch_routing_decision_for_user`

```python
class SkillRunner:
    def __init__(self, *, agent_config, event_log, workspace_root, permission_checker): ...
    async def dispatch(self, decision: RoutingDecision, *, chain_id: str) -> None: ...
    async def cancel(self, skill_name: str) -> None: ...
    async def cancel_all(self) -> None: ...
    def running_names(self) -> list[str]: ...
```

`SkillRunner` is the surface FP-0012 needs: `dispatch()` is the entry point for both
synchronous and future async skill execution. `cancel_all()` is called on session shutdown.

Target files:
- `src/reyn/chat/services/compaction_controller.py` — new file
- `src/reyn/chat/services/skill_runner.py` — new file
- `src/reyn/chat/session.py` — wire injections, remove extracted methods

### Wave 2 — MEDIUM × 2 (A2A and intervention are higher coupling, done after Wave 1 stabilizes)

**A2AHandler** (`services/a2a_handler.py`)

Extracts: `_send_to_agent`, `_send_agent_response`, `_handle_agent_request`,
`_handle_agent_response`, `_resolve_pending_chain`

```python
class A2AHandler:
    def __init__(self, *, agent_registry, event_log, chain_manager): ...
    async def send(self, target_agent: str, payload: A2APayload, *, chain_id: str) -> None: ...
    async def receive_request(self, payload: A2APayload) -> None: ...
    async def receive_response(self, payload: A2APayload) -> None: ...
```

The A2A protocol is entirely self-contained (send/receive/chain resolution). The only
coupling to `ChatSession` is the `chain_manager` dependency (already extracted).

**Note**: FP-0013 (unified-inbox-outbox-transport, ACCEPTED) restructures the transport
layer that A2A send/receive sits on. `A2AHandler` extraction should land in the same PR
as FP-0013's implementation, or immediately after — implementing this extraction before
FP-0013 lands will cause the A2A interface to require adjustment again.

**InterventionHandler** (`services/intervention_handler.py`)

**LANDED** (commit `11d96dc`): src/reyn/chat/services/intervention_handler.py

Extracts: `_maybe_answer_oldest_intervention`, `_dispatch_intervention`,
`_announce_intervention`, `_wait_for_intervention_answer`

```python
class InterventionHandler:
    def __init__(self, *, intervention_registry, event_log, skill_runner): ...
    async def maybe_answer(self, text: str) -> bool: ...
    async def dispatch(self, iv: Intervention) -> InterventionAnswer: ...
```

`InterventionHandler` depends on the already-extracted `InterventionRegistry` and on
`SkillRunner` (Wave 1). This wave therefore cannot start before Wave 1 is complete.

**A2AHandler** extraction remains **proposed** (Wave 2 part 2) — coordinates with FP-0013
which is now fully LANDED; A2AHandler should land in the next wave.

Target files:
- `src/reyn/chat/services/a2a_handler.py` — new file (proposed, next wave)
- `src/reyn/chat/services/intervention_handler.py` — LANDED commit `11d96dc`
- `src/reyn/chat/session.py` — wire injections, remove extracted methods

### Wave 3 — SMALL (cleanup wave, deferred to FP-0011 landing)

**AutoResumeHandler** (`services/auto_resume_handler.py`)

**LANDED** (commit `ba7f7c3`): src/reyn/chat/services/auto_resume_handler.py

Extracts: `_auto_resume_active_skills`

```python
class AutoResumeHandler:
    def __init__(self, *, skill_runner, event_log, wal_reader): ...
    async def resume_active(self) -> int: ...  # returns count of resumed skills
```

`AutoResumeHandler` depends on `SkillRunner` (Wave 1). Its extraction was coupled to
FP-0011 (remove narrator) because `_auto_resume_active_skills` previously called into the
narrator path. FP-0011 landed first; Wave 3 landed in the same wave (commit `ba7f7c3`).

Target files:
- `src/reyn/chat/services/auto_resume_handler.py` — LANDED commit `ba7f7c3`
- `src/reyn/chat/session.py` — wire injections, remove extracted method

### Post-Wave 3 target state

**Current position**: Wave 1+2 part 1+3 complete (2026-05-13/14). Wave 2 part 2 (A2AHandler)
remains proposed for the next wave.

```
src/reyn/chat/
├── session.py              ~600 lines   (message loop + dependency wiring only)
└── services/               ~3,800 lines total
    ├── budget_gateway.py
    ├── chain_manager.py
    ├── intervention_registry.py
    ├── memory_service.py
    ├── router_host_adapter.py
    ├── snapshot_journal.py
    ├── compaction_controller.py   ← Wave 1 (LANDED commit 6620505)
    ├── skill_runner.py            ← Wave 1 (LANDED commit 9ae66fa)
    ├── intervention_handler.py    ← Wave 2 part 1 (LANDED commit 11d96dc)
    ├── auto_resume_handler.py     ← Wave 3 (LANDED commit ba7f7c3)
    └── a2a_handler.py             ← Wave 2 part 2 (proposed, next wave)
```

`session.py` becomes a thin wiring layer: it instantiates all services in `__init__`, and
`run()` routes incoming messages to the appropriate service method. No business logic remains
in `session.py` itself.

---

## Priority ordering

**Wave 1 → Wave 2 → Wave 3**

Wave 1 (CompactionController + SkillRunner) is the minimum viable extraction: it aligns
`session.py` with the landed FP-0012 async OS and reduces the blast radius for any
session.py change. Wave 2 depends on Wave 1's `SkillRunner` being stable, and should
coordinate with FP-0013. Wave 3 is coupled to FP-0011 and can wait.

---

## Dependencies

- **Wave 1**: No external FP dependencies. Can start immediately.
- **Wave 2**: Requires Wave 1 complete (InterventionHandler depends on SkillRunner).
  `A2AHandler` extraction should coordinate with FP-0013 (unified-inbox-outbox-transport,
  ACCEPTED) — the transport layer restructure overlaps with the A2A send/receive interface.
- **Wave 3**: Requires Wave 1 complete + FP-0011 landing recommended (narrator path removal).
- **FP-0012** (async skill execution): LANDED (commit `c9e79d6`). Wave 1 makes the
  `session.py` side consistent with the landed async OS primitives.

---

## Cost estimate

| Wave | Component | Cost |
|---|---|---|
| 1 | CompactionController extraction | SMALL |
| 1 | SkillRunner extraction | SMALL |
| 2 | A2AHandler extraction | MEDIUM |
| 2 | InterventionHandler extraction | MEDIUM |
| 3 | AutoResumeHandler extraction | SMALL |
| All | Tests (Tier 1: service contracts) | SMALL |
| **Total** | **3 waves** | **MEDIUM** |

Wave 1 alone is SMALL and can ship independently. The full extraction to ~600 lines is
MEDIUM total.

---

## Alignment with Reyn principles

This FP is purely structural — no principle violations are introduced or resolved. The
motivation is maintainability and aligning `session.py` with the landed FP-0012 async
execution model (P3 / P6 clean execution).

---

## Related

- `src/reyn/chat/session.py` — extraction source (3,836 lines)
- `src/reyn/chat/services/` — existing extracted services (6 files, 2,122 lines)
- FP-0011 (`0011-remove-narrator.md`) — Wave 3 coupling (narrator path in AutoResumeHandler)
- FP-0012 (`0012-async-skill-execution.md`) — LANDED commit `c9e79d6`; Wave 1 aligns session.py with the async OS
- FP-0013 (`0013-unified-inbox-outbox-transport.md`) — ACCEPTED; Wave 2 A2AHandler extraction should coordinate with this
