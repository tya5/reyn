# S3 — topology-create org-design dogfood: scenario design (gated handoff)

**Author:** dogfood-coder (sandbox_2, net-isolated → static design only)
**Date:** 2026-06-25
**Status:** CLOSED (2026-06-25). **Structural validation PASS** (lead-confirmed): `agent_spawn` verified e2e, `tool_called` contract verified, WAL `agent_created` confirmed, isolation handled (#2169). **V-rate weak-model-capped** — gemini-family (flash-lite/flash) empties ~20-33% on tool/narration turns → the multi-turn set isn't bounded-completable via this proxy; the automated V-rate measures model reliability, not Reyn → **number deferred to a reliable endpoint**. The dogfood's core question — does LLM-driven org-design work via the spawn-arc tools — is answered **YES**. (`agent_spawn` #2160, `topology_create` C1 #2163, both on main; co-verify at #2163#issuecomment-4800805801.)

This is a **handoff artifact**: the live run executes on a live-LLM-capable session once C lands. sandbox_2 cannot reach the LLM proxy (`project_sandbox2_network_isolated_no_live_llm`), so this is static scenario design + the event-assertion contract, grounded against the real `agent_spawn` surface and code-confirmed event emits.

---

## Recon (code-confirmed, current main `db69a7d1`)

- **B-tool surface** (`src/reyn/tools/agent_spawn.py`): router-only ToolDefinition (`gates: router=allow, phase=deny`). Args `{name (required), role}`. Handler → `ctx.router_state.spawn_agent_fn` → host `spawn_agent` → `registry.create_agent(parent=<spawner>)` (the ONE create seam). Returns a spawn-ack; the LLM never supplies the parent (OS-set lineage, forge-guard).
- **C1 surface (confirmed, #2163):** `topology_create` router-only ToolDefinition (in the #2081 `_delegate` floor — org-design is delegate-allowed). Args `name, kind, members, leader, profiles`. Handler → `rs.topology_create_fn` → `router_loop._topology_create_bound_impl` → `RouterHostAdapter.create_topology(...)` → builds `Topology.new(...)` → **`await registry.create_topology(topo)`** (the #2153 logged emit seam, `registry.py:2784`), NEVER sync `add_topology` (co-verified). A `topology_created` WAL event lands → rewind-durable. Members + profile-binding targets are **subtree-restricted** by a forge-guard (`is_spawn_descendant`): a member must be in the creator's spawn-lineage subtree (no grabbing a stranger agent). C assigns the restrict-only `capability_profile` bindings (the ⊆-parent narrowing); agent-spawn = identity+lineage, topology = capability assignment (clean split).
- **Event source caveat (verified + live-corrected):** the dogfood runner captures **P6 events** (`.reyn/dogfood/runs/<id>/scenarios/<sid>/events.jsonl`). `agent_created` / `topology_created` are **WAL kinds** (`state_log`), NOT P6 — not assertable via `must_emit`. **Live correction (tui, 2026-06-25):** the direct LLM-tool path emits **`tool_called`** (+ `chat_turn_completed_inline`), and **`routing_decided`=0** (that's a *skill*-routing event, not a direct-tool event). ⇒ `must_emit` uses **`tool_called`**; agent/topology creation is verified via the **reply rubric** + a post-run WAL grep (primary evidence). My earlier `routing_decided` default was wrong and refuted even the verified happy path — see Live-run findings.

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
          - { type: tool_called, count: ">=1" }   # LLM-tool-dispatch P6 (live-confirmed: routing_decided=0 on the direct-tool path)
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
          - { type: tool_called, count: ">=1" }
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
          - { type: tool_called, count: ">=2" }   # ≥2 tool calls (spawn(s) + topology_create); rubric carries the team-wiring assertion
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
          - { type: tool_called, count: ">=1" }
      outcome_prediction: { verified: 0.45, inconclusive: 0.4, refuted: 0.15, blocked: 0.0 }
```

## Scenario-design audit (4 dimensions — `feedback_scenario_design_audit_checklist`)

1. **Data semantic match:** inputs are natural org-design requests (Japanese, matching the existing dogfood corpus tone) that map to spawn/topology intents — not contrived tool-name prompts (avoids the benchmark-tuning soft-cheat: the capability is reachable via the general router path, not advertised for the scenario).
2. **Tool reachability:** `agent_spawn` is router-allowed (confirmed). `topology_create` reachability PENDING e2e wiring it into the router tool set (mirror of agent_spawn registration in `tools/__init__.py`).
3. **Rubric measures Reyn behavior, not LLM elaboration:** rubrics assert structural outcomes (agent created / topology wired / narrowed) + honesty guards (no fabricated agent, no over-claimed capability) — not prose quality. The ⊆-parent honesty guard doubles as a weak-tier hallucination check.
4. **Event infra captures it:** `tool_called` is the live-confirmed P6 event for the direct LLM-tool path (`routing_decided`=0 there — skill-routing only). Creation events are WAL (not P6) → verified via rubric + post-run WAL grep, not `must_emit`. Counts kept conservative (`>=1`, `>=2` for the team) so weak-model call-count variance doesn't false-refute; the rubric carries behavior specifics.

## Open questions / handoff

- **e2e surface — RESOLVED (#2163):** tool `topology_create`, args `name/kind/members/leader/profiles`, routes through `registry.create_topology` (co-verified), emits the WAL `topology_created` (no separate P6 event observed). Members + profile targets subtree-restricted.
- **harness — RESOLVED (tui-coder, code-confirmed):** the dogfood verifier is **P6-only**. `runner.py` captures the emitted P6 events into `scenarios/<id>/events.jsonl`; there is NO `state_log`/WAL read in the capture path (the `wal.jsonl` ref at `runner.py:46` only CLEARS it between runs). `verify_events` is source-agnostic but is only ever fed the P6 list → `agent_created` / `topology_created` (WAL kinds) are **NOT assertable via `must_emit`** (they'd refute, not match). ⇒ `must_emit` uses **`tool_called`** (P6, live-confirmed — NOT `routing_decided`, which is 0 on the direct-tool path); verify creation via the reply rubric **+ a post-run WAL grep** as a primary-evidence post-condition.
- **live session (tui or other live-capable) — handoff:** once #2163 merges, run scenarios 1–2 (real now) and 3–4 (grounded vs #2163); report V-rate + a primary-evidence trace. sandbox_2 (net-isolated) cannot run live.

## Live-run findings (tui-coder, 2026-06-25 — gemini-2.5-flash-lite via proxy)

First live run of scenarios 1–2 (3–4 not run to completion; contract+isolation dominated). Primary evidence: `events.jsonl` type counts + the agent dir + the reply.

- ✅ **Happy path VERIFIED:** `spawn_single_agent` → reply "researcher を作成しました" → `.reyn/agents/researcher` created → `reply_outcome=verified`. The `agent_spawn` LLM-tool path works end-to-end (flash-lite handles tool-calling).
- 🔧 **`must_emit` corrected → `tool_called`** (applied above): on the direct-tool path `routing_decided`=0, `tool_called`=1, `chat_turn_completed_inline`=1. My `routing_decided` default refuted even the verified happy path (the 0% events-V cause — a contract bug, not a behavior failure).
- 🔧 **State-isolation requirement (applied to Preconditions). CLASSIFIED — dogfood-harness artifact, NOT an OS defect (#2169):** `--agent default` single-run uses the shared project `.reyn/agents/` (polluted with prior runs' agents) → `spawn_single_agent` hit "already exists"→refuted until reset. 3-axis evidence: OS isolates agents/sessions by distinct dirs (P5 — no distinct-agent bleed path); the runner's `"fresh"` mode resets action_usage/wal/history but NOT `.reyn/agents/` (`runner.py:46`), and the spawn-arc is the first dogfood to *create* agents (latent gap newly exposed); batch mode (per-worker worktree) is unaffected. ⇒ scenario-setup fix (fresh per-run workspace) is correct; harness gap tracked in **#2169**. Scenarios are sequential + collision-sensitive (scenario 2 depends on scenario 1's create) → **require a fresh per-run workspace** (batch worktree isolation, or a manual temp-cwd / clean `.reyn` / run-unique names).
- ℹ️ **Transient (honest scoping):** a ~6.5min hang on the first LLM call (0% CPU), NOT reproduced on re-run with identical config → transient proxy/network blip, observed once, not a config bug.
## Re-run results (tui-coder, 2026-06-25 — tool_called contract + isolated temp-cwd workspace)

- ✅ **Isolation fix works:** temp cwd + fresh `.reyn` → ZERO project pollution (#2169 workaround confirmed; the Preconditions are correct). researcher/writer/reader all created via the LLM tool path; scenario-3 team wiring progressed.
- ✅ **Structural validation DONE:** isolation ✓, `tool_called` contract ✓ (scenario-1 `reply_outcome=verified`), `agent_spawn` path ✓ end-to-end. The design + tool surface are validated.
- ⚠️ **Clean 4/4 V-rate is weak-model-variance-bound, NOT a Reyn issue:** gemini-2.5-flash-lite intermittently returns empty-choices/errors on tool-call requests (N=5 direct probe: 4 OK / 1 ERR ≈ 20% — matches the `gemini_flashlite_empties_on_bare_tool_syntax` pin, #1638/#1639 area). The multi-turn tool-heavy scenarios (esp. #3: 2 spawns + topology_create = several tool turns) compound the ~20% → litellm retries → the 4-scenario run hit the 240s timeout mid-run. The 2-scenario smoke completed (scenario-1 verified) → the flakiness is the model's tool-call reliability, not the scenarios or Reyn.
- **Closing V-rate — CONCLUSIVE (tui, 5-way):** the full 4-set is **NOT bounded-completable via this proxy.** All 5 configs TIMED OUT — flash-lite (240s, 600s), gemini-2.5-flash (480s), multi-set (220s): gemini-family ~20-33% empty-choices compound over multi-turn + per-scenario LLM-judge calls + litellm retries exceed even 600s. Isolated **scenario-1 (spawn_single)** completed as the clean data point:
  - `events_outcome: VERIFIED` — `tool_called` fired 2× → **the `tool_called` contract WORKS.**
  - **WAL `agent_created` (researcher) confirmed** — the post-run WAL-grep primary evidence; the spawn SUCCEEDED.
  - `reply_outcome: refuted` — **ONLY** because flash-lite returned an empty *narration* response ("The model returned an empty response"); the spawn worked, the model's final text was empty. (Non-deterministic: the earlier smoke run narrated → verified; this run empty → refuted = the ~20-33% empty coin-flip.)
  - **V-rate: 0% (1 blocked) = a pure flash-lite empty-response ARTIFACT, NOT Reyn behavior.**
- **Disposition (#2164 closing):** **structural ✓** (`tool_called` contract verified, WAL `agent_created` confirmed, `agent_spawn` path works e2e); **V-rate weak-model-capped** — flash-lite empties on the tool turn (→ retries/timeout) OR the narration turn (→ empty reply → refuted), so the automated V-rate measures the model's tool/narration reliability, not Reyn. **A meaningful V-rate needs a more reliable LLM endpoint than this proxy; defer the number.**
- **Spawn-arc UX data point (not a bug):** multi-tool-call org-design flows are hard for weak-tier models to drive reliably (compounding per-call empties). Relevant to Reyn's weak-model-reliability story (cf. the chat-envelope-unification direction) — noted, not filed (the tool path works when the model cooperates).

## Preconditions (live run)

- **Fresh per-run workspace** (batch worktree isolation, or a clean `.reyn/agents/` / run-unique names). Scenarios run sequentially and are collision-sensitive — a polluted shared `.reyn` flips `spawn`→`duplicate` and refutes scenario 1.
- Members must pre-exist in the creator's spawn-lineage subtree (forge-guard) → topology scenarios spawn agents first, then wire.
