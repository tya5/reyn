# S8 Retest — drop_source via chat + permission gate (Batch 18)

| Field | Value |
|---|---|
| Date | 2026-05-10 |
| main HEAD | `9681096` (= post fix-wave) |
| Scenario | S8 retest — drop_source via chat |
| Sample size | N=3 (mini-retest) |
| **Verdict breakdown** | **verified: 0 / refuted: 0 / inconclusive: 3 / blocked: 0** |

## 1. Summary Table

| 項目 | 予測 | 実測 |
|---|---|---|
| verified | 75% | 0/3 (0%) |
| refuted  | 20% | 0/3 (0%) |
| inconclusive | 5% | **3/3 (100%)** |
| blocked  | 0% | 0/3 (0%) |
| **drop_source invoked** | — | **3/3 (100%)** |
| **permission_denied event** | — | **3/3 (100%)** |
| cleanup completed | — | 0/3 (gated by deny path) |

実測 4-class Brier: `(0-0.75)² + (0-0.20)² + (1-0.05)² + (0-0)² = 0.5625 + 0.04 + 0.9025 + 0 = 1.505`

## 2. Per-Run Details

| Run | agent | tool_called | drop_source args | perm event | post manifest | post db_chunks | verdict | reply (head) |
|---|---|---|---|---|---|---|---|---|
| 1 | b18_s8_run1 | `drop_source` | `{source: test_drop}` | `permission_denied(index_drop)` | True | 3 | inconclusive | "I am unable to remove the `test_drop` source from the index as it has been denied." |
| 2 | b18_s8_run2 | `drop_source` | `{source: test_drop}` | `permission_denied(index_drop)` | True | 3 | inconclusive | "The request to remove the `test_drop` source was denied, likely due to a user confirmation step…" |
| 3 | b18_s8_run3 | `drop_source` | `{source: test_drop}` | `permission_denied(index_drop)` | True | 3 | inconclusive | "I am unable to remove the 'test_drop' source from the index because I do not have the necessary permissions." |

Each run used a fresh agent (`b18_s8_run{1,2,3}`) created via `POST /api/agents`, so chat history did not bleed across runs. Pre-state for every run: `manifest=True, db_chunks=3` (seed visible cross-process via mtime-poll fix).

## 3. Event Trace (representative — run 1)

```
user_message_received   "Remove the test_drop source from the index"
tool_called             {tool: drop_source, args: {source: test_drop}}
permission_denied       {kind: index_drop, reason: "Index drop of 'test_drop' denied by user."}
tool_returned           {result: {kind: index_drop, status: denied, error: "..."}}
```

All 3 fix-wave bugs are demonstrably resolved:

- **B17-S8-2 (CRITICAL, fixed in `0014310`)** — `drop_source` is registered in
  `build_tools()` H-section + `_REGISTRY_DISPATCH_TOOLS` frozenset; LLM sees the
  tool, invokes it correctly, and dispatch routes to the op handler (3/3).
- **B17-S8-1 (HIGH, fixed in `d670839`)** — SourceManifest mtime poll makes the
  seeded `test_drop` source visible to the running web process without restart;
  `format_for_prompt()` returns the live entry, LLM context shows it (3/3 reply
  reasoning correctly references the source as existing).
- **B17-S8-3 (HIGH, fixed in `fa05e8c`)** — `_make_router_op_context()` and
  `RouterHostAdapter._build_op_context()` now set `PermissionDecl.index_drop=True`,
  so `require_index_drop()` Step 1 (decl guard) passes and the resolver actually
  evaluates the request against config / approvals (3/3 — gate reaches resolver).

## 4. Permission Gate Verification

- **Gate engagement: 3/3** — `permission_denied` event fires for every run with
  `kind: index_drop`, `reason: "Index drop of 'test_drop' denied by user."`. This
  proves the gate is reachable end-to-end: tool dispatch → handler →
  `require_index_drop` → decl guard pass → config check (no allow) → saved/session
  miss → `interactive=False` → deny.
- **Ask-and-approve path: 0/3** — never observed because the running `reyn web`
  server constructs `PermissionResolver(interactive=False)` (web/deps.py L130).
  In non-interactive mode `_approve()` short-circuits to `False` at L300 without
  calling `bus.request()`, so the `ChatInterventionBus` is wired but unused for
  permission asks. The "answer y" path requires either:
  1. `permissions.index_drop: allow` in config (config-approved fast path), OR
  2. `REYN_INDEX_DROP_AUTO_APPROVE=1` env var on the server process, OR
  3. Pre-persisted `index_drop:test_drop: true` in `.reyn/approvals.yaml`.
  None of these can be applied to the running server without a restart, since
  `_perm_resolver` is a process-level singleton with `_saved` and `_config`
  loaded at construction (`web/deps.py` `_get_perm_resolver()`).

## 5. What Happened

**Tool routing → permission gate → audit trail all work correctly.** The
denial behaviour is architecturally correct given web's `interactive=False`
posture: a destructive op without a pre-approved policy must deny rather than
silently allow. The LLM's reply also handles the `tool_returned status=denied`
gracefully — it summarizes the deny truthfully without hallucinating success.

**verdict=inconclusive (not refuted)** because the chat path executed exactly
as designed; only the cleanup half is gated by the deny default. Calling this
"refuted" would mis-attribute a config issue to the chat path.

## 6. Calibration Delta

Predicted distribution assumed an interactive ask-and-approve cycle was
reachable via stdin pipe (per task spec). In practice the A2A web server is
non-interactive by design, so "verified" with cleanup was unreachable without
config / env mutation that I cannot apply to the running process.

| 予測 | 実測 | Brier component |
|---|---|---|
| verified 75% | 0/3 (0%) | (0.75)² = 0.5625 |
| refuted 20%  | 0/3 (0%) | (0.20)² = 0.04 |
| inconclusive 5% | 3/3 (100%) | (0.95)² = 0.9025 |
| blocked 0% | 0/3 (0%) | 0 |
| **4-class Brier** | — | **1.505** |

The miss is calibration-against-environment, not against the bug fixes
themselves. All three target bugs are demonstrably fixed at the layer the
fix-wave aimed at (router build_tools, dispatch frozenset, op context
PermissionDecl, manifest mtime). What remains is **whether `reyn web` should
expose a non-interactive auto-approve path or a permission-ask round-trip over
A2A** — that is a release-readiness UX question, not a regression.

## 7. Carry-Over

- **R1 [open, MED] — A2A non-interactive permission UX**. With
  `interactive=False`, any destructive op declared as `ask` default cannot be
  approved through the chat round-trip. Web users have no path to grant a
  one-shot approval short of editing `reyn.yaml` (`permissions.index_drop: allow`)
  or `.reyn/approvals.yaml` and restarting. Options:
  1. Wire `ChatInterventionBus` for permission asks too (skip the
     `interactive=False` short-circuit when a bus is supplied).
  2. Surface a sidecar `/api/approvals` endpoint to write+reload
     approvals.yaml at runtime (pop the singleton).
  3. Document the `REYN_INDEX_DROP_AUTO_APPROVE=1` env-var path as the
     supported CI / dogfood approval method.
- **R2 [open, LOW] — verified-path regression test**. Re-run S8 once after the
  next server restart with `permissions.index_drop: allow` to confirm the
  cleanup half closes. Not a fix-wave gap; environmental.
- **No new bugs surfaced in this scenario.** Tool routing, manifest mtime
  poll, decl guard all behave as fix-wave intended.
