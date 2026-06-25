# S3 — topology-create org-design dogfood: scenario design (gated handoff)

**Author:** dogfood-coder (sandbox_2, net-isolated → static design only)
**Date:** 2026-06-25
**Status:** DESIGN — grounded against the real surface. `agent_spawn` (B-tool, #2160) is live on main; `topology_create` (C1) is in **PR #2163** (in review, pending lead merge — seam-owner co-verify PASS at #2163#issuecomment-4800805801). Scenarios below are now grounded against #2163's confirmed surface; the live run waits on #2163 merging (sandbox_2 is net-isolated → hand to a live-capable session, e.g. tui).

This is a **handoff artifact**: the live run executes on a live-LLM-capable session once C lands. sandbox_2 cannot reach the LLM proxy (`project_sandbox2_network_isolated_no_live_llm`), so this is static scenario design + the event-assertion contract, grounded against the real `agent_spawn` surface and code-confirmed event emits.

---

## Recon (code-confirmed, current main `db69a7d1`)

- **B-tool surface** (`src/reyn/tools/agent_spawn.py`): router-only ToolDefinition (`gates: router=allow, phase=deny`). Args `{name (required), role}`. Handler → `ctx.router_state.spawn_agent_fn` → host `spawn_agent` → `registry.create_agent(parent=<spawner>)` (the ONE create seam). Returns a spawn-ack; the LLM never supplies the parent (OS-set lineage, forge-guard).
- **C1 surface (confirmed, #2163):** `topology_create` router-only ToolDefinition (in the #2081 `_delegate` floor — org-design is delegate-allowed). Args `name, kind, members, leader, profiles`. Handler → `rs.topology_create_fn` → `router_loop._topology_create_bound_impl` → `RouterHostAdapter.create_topology(...)` → builds `Topology.new(...)` → **`await registry.create_topology(topo)`** (the #2153 logged emit seam, `registry.py:2784`), NEVER sync `add_topology` (co-verified). A `topology_created` WAL event lands → rewind-durable. Members + profile-binding targets are **subtree-restricted** by a forge-guard (`is_spawn_descendant`): a member must be in the creator's spawn-lineage subtree (no grabbing a stranger agent). C assigns the restrict-only `capability_profile` bindings (the ⊆-parent narrowing); agent-spawn = identity+lineage, topology = capability assignment (clean split).
- **Event source caveat (verified):** the dogfood runner captures **P6 events** (`.reyn/dogfood/runs/<id>/scenarios/<sid>/events.jsonl`). `agent_created` / `topology_created` are **WAL kinds** (`state_log`), NOT P6 event-log types (`event_schema.py` has `routing_decided`, `chat_turn_completed_inline`, …). ⇒ `must_emit` reliably asserts `routing_decided`; agent/topology creation is verified via the **reply rubric** + a WAL/state post-condition (the latter pending confirmation that the harness can read WAL — see Open Questions).

## C review gates to exercise as dogfood angles (per lead, 2026-06-25)

1. **MUST-1 routing** (#2153): `topology_create` host fn routes through `create_topology` (WAL-tracked) — a topology is fully-tracked-or-untracked, else rewind reconstruction diverges. *Dogfood angle:* create a topology via the tool, then a rewind across the create reconstructs it faithfully (rewind is a slash/op, not LLM-driven → likely a Tier-2 test, not a dogfood scenario; noted for completeness).
2. **Fail-closed on absent referent** (#2161 generalization): an absent referent in C's cap-walk (profile binding to a purged/archived-gone member, or a missing profile) must FAIL CLOSED (compose floor / deny), distinguished from present-but-unrestricted by an existence check (B-tool's `(self._dir/parent).is_dir()` shape). *Dogfood angle:* hard to drive via LLM input — better a Tier-2 test in e2e's C PR; flagged.
3. **Live-prune ↔ rewind-prune symmetry** (#2159 companion): purging a member live-prunes its topology edges + profile binding (cascade-emit), not only rewind-rebuild. Covered by #2159 + e2e's C; not a dogfood scenario.

---

## Scenario set (draft)

```yaml
---
type: dogfood_scenario_set
name: org_design_topology
description: >
  LLM-driven org design — spawn agents under authority (agent_spawn) and wire a
  topology (topology_create): who-can-message-whom + capability narrowing. Exercises
  the #2103 B/C org-design tool surface. GATED on topology_create (C) landing.
covers:
  - multi-agent/agent-spawn
  - multi-agent/topology-create        # PENDING C
  - permissions/capability-profile-narrowing  # PENDING C

scenarios:
  # ── 1. agent_spawn — single agent (REAL surface, groundable now) ───────────
  - id: spawn_single_agent
    covers:
      - multi-agent/agent-spawn
    input: "リサーチ担当のエージェント 'researcher' を一人作って。"
    expected:
      reply:
        kind: judge
        rubric:
          - reply confirms a new agent named 'researcher' was created under the user's authority
          - reply does NOT claim a capability the spawner lacks (= ⊆-parent honesty; no over-promise)
      events:
        must_emit:
          - { type: routing_decided, count: ">=1" }   # code-confirmed P6 event
      outcome_prediction: { verified: 0.6, inconclusive: 0.3, refuted: 0.1, blocked: 0.0 }

  # ── 2. agent_spawn — duplicate-name rejection (error path, groundable) ─────
  - id: spawn_duplicate_rejected
    covers:
      - multi-agent/agent-spawn
    input: "'researcher' という名前のエージェントをもう一つ作って。"  # after #1 (same name)
    expected:
      reply:
        kind: judge
        rubric:
          - reply reports the name is already taken (= agent_exists error surfaced honestly)
          - reply does NOT silently fabricate a second 'researcher'
      events:
        must_emit:
          - { type: routing_decided, count: ">=1" }
      outcome_prediction: { verified: 0.55, inconclusive: 0.3, refuted: 0.15, blocked: 0.0 }

  # ── 3. topology_create — team wiring  (grounded vs #2163) ──────────────────
  # NOTE: members must be in the creator's spawn-lineage subtree (forge-guard) →
  # the scenario spawns researcher + writer FIRST (agent_spawn), THEN wires them.
  # topology_created is a WAL kind (not P6) → assert creation via rubric + a WAL
  # post-condition (see Open Questions on harness WAL-read); must_emit stays P6.
  - id: design_team_topology
    covers:
      - multi-agent/agent-spawn
      - multi-agent/topology-create
    input: "researcher と writer の2人チームを作って、writer から researcher にメッセージできるように。"
    expected:
      reply:
        kind: judge
        rubric:
          - reply confirms a team topology with researcher + writer was created
          - reply describes the messaging wiring (writer → researcher) it set up
      events:
        must_emit:
          - { type: routing_decided, count: ">=2" }   # spawn(s) + topology_create turns
      outcome_prediction: { verified: 0.5, inconclusive: 0.35, refuted: 0.15, blocked: 0.0 }

  # ── 4. topology_create — capability narrowing  (grounded vs #2163) ─────────
  # The ⊆-parent narrowing via the `profiles` arg (member→capability_profile binding).
  # Profile-binding target is subtree-restricted (same forge-guard). The bound child
  # is narrowed BELOW the spawner; resolved_profile_for composes the binding as a
  # restrict-only conjunct. C2 (deferred) adds the fail-closed-on-absent-referent walk.
  - id: narrow_member_capability
    covers:
      - multi-agent/topology-create
      - permissions/capability-profile-narrowing
    input: "ファイル読み取りだけできる(shell 不可の)サブエージェント 'reader' を作って。"
    expected:
      reply:
        kind: judge
        rubric:
          - reply confirms a capability-narrowed sub-agent (read-only, no shell) was created
          - reply does NOT grant the child a capability beyond read-only (= no-escalation honesty)
      events:
        must_emit:
          - { type: routing_decided, count: ">=1" }
      outcome_prediction: { verified: 0.45, inconclusive: 0.4, refuted: 0.15, blocked: 0.0 }
```

## Scenario-design audit (4 dimensions — `feedback_scenario_design_audit_checklist`)

1. **Data semantic match:** inputs are natural org-design requests (Japanese, matching the existing dogfood corpus tone) that map to spawn/topology intents — not contrived tool-name prompts (avoids the benchmark-tuning soft-cheat: the capability is reachable via the general router path, not advertised for the scenario).
2. **Tool reachability:** `agent_spawn` is router-allowed (confirmed). `topology_create` reachability PENDING e2e wiring it into the router tool set (mirror of agent_spawn registration in `tools/__init__.py`).
3. **Rubric measures Reyn behavior, not LLM elaboration:** rubrics assert structural outcomes (agent created / topology wired / narrowed) + honesty guards (no fabricated agent, no over-claimed capability) — not prose quality. The ⊆-parent honesty guard doubles as a weak-tier hallucination check.
4. **Event infra captures it:** `routing_decided` is code-confirmed P6. Creation events are WAL — see Open Questions; do not over-assert WAL kinds in `must_emit` until the harness's WAL-read support is confirmed.

## Open questions / handoff

- **e2e surface — RESOLVED (#2163):** tool `topology_create`, args `name/kind/members/leader/profiles`, routes through `registry.create_topology` (co-verified), emits the WAL `topology_created` (no separate P6 event observed). Members + profile targets subtree-restricted.
- **harness — RESOLVED (tui-coder, code-confirmed):** the dogfood verifier is **P6-only**. `runner.py` captures the emitted P6 events into `scenarios/<id>/events.jsonl`; there is NO `state_log`/WAL read in the capture path (the `wal.jsonl` ref at `runner.py:46` only CLEARS it between runs). `verify_events` is source-agnostic but is only ever fed the P6 list → `agent_created` / `topology_created` (WAL kinds) are **NOT assertable via `must_emit`** (they'd refute, not match). ⇒ Keep `must_emit: routing_decided` (P6); verify creation via the reply rubric **+ a post-run WAL grep** (`.reyn/.../wal.jsonl` for `topology_created`/`agent_created`) as a primary-evidence post-condition. No scenario YAML change needed.
- **live session (tui or other live-capable) — handoff:** once #2163 merges, run scenarios 1–2 (real now) and 3–4 (grounded vs #2163); resolve the harness-WAL-read question; report V-rate + a primary-evidence trace. sandbox_2 (net-isolated) cannot run live.
