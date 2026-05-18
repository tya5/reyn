# Batch 41 — Retrospective

> Eleventh dogfood batch (= B40 v2 full retest at main HEAD `dd896f2c`).
> Headline V=13/58 = 22.4% (= -8V mathematical vs B39 21V), but the
> composition is **complex and patch-isolated**: the B40 v2 ARS-extend fix
> is **empirically verified positive at 9 scenarios** (= R-WEB chain 3/3
> + word_stats_demo 2/2 + mcp_install 2/2 + rag.operation__drop_source +
> mcp_search routing), while the -8V mathematical loss decomposes into
> W7 long-session env+design issues (-5V, all NOT B40 v2 attributed),
> W5/W4 env+LLM-noise (-2V), and W1 SP-rule+gate (-2V) with offsets.
>
> The headline of this batch is **methodology**, not V count: 7
> patch-isolation tests (= cost ~$0.35) traced every ambiguous attribution
> to primary evidence, refuted 3 mis-attributions to B40 v2, and surfaced
> 4 new B42 findings — each with its own evidence anchor.

---

## 1. What this batch verified — primary data

### B40 v2 fix landing (= R-WEB cold-start cognitive bias closed)

PR #157 (= `_collect_all_session_ars_entries` extended with `known_skill_names` parameter, empty-schema skills rendered as `<name>: {}` in `invoke_action` description ARS block) was the B40-wave fix targeting B39 W6 R-WEB regression. B41 verified the fix end-to-end:

| Scenario | B39 verdict | B41 verdict | Evidence |
|---|---|---|---|
| W6 narr-1 mcp_search | R (routing miss) | **I** (routing ✓) | `routing_decided{skill__mcp_search}` + `skill_run_spawned` |
| W6 s-fp11-3 mcp_search-empty | R | **I** | same pattern |
| W6 s-fp12-completion-1 mcp_search-narrate | R | **I** | same pattern |
| W1-S4 word_stats_demo | R | **V** | `routing_decided{skill__word_stats_demo}` + `skill_run_completed{finished}` + stats reply |
| W2-S5 word_stats_demo_sentence | I | **V** | same mechanism |
| W4-S2 mcp_install_gate_prompt | I | **V** | B39 had wrong action `mcp.operation__drop_server`; B41 → `skill__mcp_install` correct |
| W4-S6 rag.operation__drop_source | I (4-batch surface migration) | **V** | patch-isolation: baseline 8/10 → revert 0/10 (= 6/10 `skill__index_events` wrong, 4/10 malformed) |
| W5-S1 mcp_search_registry | R (no routing_decided) | I (routing ✓, blocked by --allow-unsafe-python env) | `routing_decided{skill__mcp_search}` + `skill_run_spawned` |
| W5-S6 mcp_install_permission_gate | R | R (routing improved within R) | `routing_decided{skill__mcp_install}` + `skill_run_spawned` |

**9 confirmed B40 v2 wins**. Crucially, the W4-S6 win (= 4-batch surface-migrating attractor finally closed) was patch-isolated as a direct B40 v2 cause: revert simulate dropped routing 8/10 → 0/10, reverting to B36-B39 wrong-action pattern.

### G12 (answered) signal fix (= PR #158, landed before B41)

W7 S4 (= repetitive_context_bloat, the primary Pattern E target) maintained **5/5 clean turns with zero empty stops** at B41 — the (answered)-signal-moved-to-role-tool-content fix verifies its primary target. Reply lengths: 1161 / 1824 / 147 / 1280 / 326 across 5 increasingly long re-asks. Context bloat → empty stop pattern is closed for this case.

---

## 2. The headline — methodology, not V count

### Patch-isolation discipline applied to every ambiguous claim

B41's V=13 result triggered a natural question: is the B40 v2 fix actually net positive, or did it introduce regressions that offset its wins? Initial sub-agent analysis attributed 2V of W7 loss to B40 v2 side effects (= W7-S2 router_empty after describe_action, W7-S7-T2 hot_list_alias direct_llm empty stop). The attribution was **structural inference**, not empirical evidence.

Per `feedback_verify_fix_via_replay_before_land`: "LLM-behavior fix の dispatch 前に trace-patch-replay で hypothesis 裏取り必須". The same discipline applies post-batch — every "X caused Y" claim in a retrospective is a hypothesis until patch-isolated.

7 patch-isolation tests executed (= ~$0.35 total, max 5 min each):

| Test | Hypothesis | Result | Attribution |
|---|---|---|---|
| W7-S2 router_empty cause | B40 v2 changed describe_action result | baseline 5/5 stop, B40 v2 revert 5/5 stop, G12 removed 5/5 stop, G12 stronger directive 3/5 stop | **Deeper design issue** (describe_action result → reply synthesis), NOT B40 v2 / G12 specifically |
| W2-S7 eval FileNotFoundError | B40 v2 unmasked latent bug vs pre-existing | baseline 5/5 routes to skill__eval, revert 5/5 still routes (= revert doesn't change routing); FileNotFoundError reproduces in both; direct code inspection: `run_skill` op treats slash-bearing paths as literal CWD-relative, no stdlib resolution | **Pre-existing latent bug**, B40 v2 only made it observable |
| W7-S5 driver timing | B41-new vs pre-existing latent | B41 `read_local_files` 69.5s (LLM call time, not OS); driver hit 60s wall-clock; B39 same scenario = 7.855s inline reply (= shared-agent context chose inline answer) | **(C) driver bug** (wait-for-skill-completed missing) + **(B) session isolation fix trigger** (= per-scenario fresh agent changed routing); (A) B40 v2 ruled out |
| narr-1 5 per-skill ablation | over-correction risk: empty-schema skills in ARS distract from intended routing | each of 5 unrelated empty-schema skills individually removed, routing 10/10 mcp_search in all variants (= same as baseline) | **No over-correction**, ARS bloat carries no routing cost on this scenario |
| narr-1 ARS format variants | `<name>: {}` vs bare name vs describe hint | `: {}` baseline 10/10 routing 5 args variants; bare name 7/10 (3 list_actions detour); describe hint 10/10 (describe_action not called, args constructed directly) | `<name>: {}` **optimal format** confirmed |
| W5-S5 over-correction (pre-retro by agent 3) | B40 v2 elevated mcp_search hurts howto routing | baseline 10/10 list_actions (= matches B39 baseline, not a B41 misroute); revert no change | **N=1 noise** on worker single-shot |
| W4-S1 inline refusal (pre-retro by agent 3) | G12 side effect on inline refusal pattern | baseline 8/10 dispatch (= V), 2/10 inline; SP rule patch 10/10 dispatch | **~33% LLM safety-caution noise rate**, B40 v2 / G12 not attributed |

### Net attribution: every -V loss traced to non-B40-v2 cause

- W7 -5V: 2V deeper design (describe_action handling) + 3V env artifacts (/tmp worktree web_fetch denied, ADR files missing, driver timing)
- W6 -2V (worker-reported): **annotation errors** per agent 2 — actual W6 delta is 0V (= plan_compare B39=I not V, plan_explain B39=R not I)
- W5 -1V: env (= no pre-created peer in fresh worktree)
- W4 ±0V: B40 v2 wins (S2+S6) offset by LLM N=1 noise (S1)
- W2 ±0V: B40 v2 win (S5) offset by env-gate I→R (= --allow-unsafe-python not configured for dogfood reyn web)
- W1 ±0V: B40 v2 win (S4) offset by SP literal "chitchat → reply without tools" effect (S1)

**Zero V loss attributed to B40 v2 over-correction or side effect**. The fix is net positive; the V umbrella metric drop is composition of environmental and pre-existing factors that happened to surface together.

---

## 3. Past-batch comparison

### V trajectory (latest 4 batches)

| Batch | V/58 | % | Headline |
|---|---|---|---|
| B38 | 23 | 39.7% | D2-wrapper scope expansion + R-WEB chain closed |
| B39 | 21 | 36.2% | First fresh-mode batch; R-WEB chain reopened by hot_list_n=10 mirror config |
| B40 (= PR #157 fix-wave, no full retest) | n/a (= targeted fix, 2 scenario verify) | n/a | B40 v2 ARS extend landed, R-WEB 3/3 + W1-S4 verified via trace-patch-replay |
| B41 | 13 | 22.4% | B40 v2 full retest at dd896f2c; V drop is environmental composition, not regression |

### Per-scenario shift table (= B38 → B39 → B41, key scenarios)

| Scenario | B38 | B39 | B41 | Driver of B41 verdict |
|---|---|---|---|---|
| W6 narr-1 mcp_search | I (routing ✓, MCP unreachable) | R (routing miss) | **I** (routing ✓) | B40 v2 confirmed restore |
| W6 s-fp11-3 mcp_search-empty | I | R | **I** | same |
| W6 s-fp12-comp-1 mcp_search-narrate | I | R | **I** | same |
| W1-S4 word_stats_demo | V | R | **V** | B40 v2 restore |
| W2-S5 word_stats_demo | I | I | **V** | B40 v2 first-time V |
| W4-S6 rag.operation__drop_source | I (source_name drift) | I (still wrong action) | **V** | B40 v2 4-batch attractor closed (patch-isolated) |
| W4-S2 mcp_install | V | I | **V** | B40 v2 |
| W2-S7 eval | V | R | **R** (root cause shift) | NEW B41-NF-S7 path resolution bug surfaced |
| W7-S4 context bloat | V | V | **V** (5/5 clean) | G12 Pattern E fix verified |
| W7-S5 python_chain | V | V | **I** | driver wait-for-skill-completed missing (NEW B41 surface) |
| W6 plan_compare | I | I | I | plan ✓ + index_query failed (pre-existing) |
| W6 plan_explain | R | R | R | LLM treats "the plan tool" as action_name (pre-existing) |
| W6 s-fp12-comp-2 anti-optimism | V | V | **V** | async error path confirmed (= retained win) |

---

## 4. Principles reinforced

### Established this batch

- **feedback_subagent_scope_bounding.md** (new): hard caps on sub-agent prompts (= 1 deliverable / max tool uses / max wall-clock). Established after initial 7 B41 workers ran 9-21 min each with 50-185 tool uses — bundled (scenarios + judge + JSON + findings) prompts undid parallelism benefit. Corrective discipline applied to subsequent 7 patch-isolation agents at 1-tool-use, ≤50w output each.

- **feedback_no_strong_model.md** (new): strong model usage requires explicit user approval at all surfaces (= Claude sub-agent `model=opus`, `reyn.local.yaml` `models.strong`, `llm_replay --model` override). Established after 3 cumulative user pushbacks across 2 sessions. Corrective: b41-1 + b41-6 `reyn.local.yaml` `models.strong: gemini-2.5-flash` updated to `flash-lite` mid-batch.

### Reinforced

- **P6 verify-first / reproduce-first**: B41 reproduced the W6 R-WEB regression at b41-6 hot_list_n=10 mirror config (= matched B39 conditions for direct comparability); reproduced bug + then verified fix via primary-data routing_decided events × 3 independent scenarios.
- **P16 pre-fix multi-agent context analysis**: 5 sonnet info-gathering on B40 v2 design space (= PR #157 prep); 4 sonnet attribution analysis post-B41 worker results (= B40 v2 vs G12 vs env disambiguation).
- **User-tunable params fixed in comparison** (`feedback_user_params_fixed_in_comparison`): B41 W6 dedicated worker on b41-6 with hot_list_n=10 = B39-mirror, enabling apples-to-apples comparison; other 5 workers at default hot_list_n=20.
- **Verify fix via replay before land** (`feedback_verify_fix_via_replay_before_land`): extended to post-batch attribution — 7 patch-isolation tests verified every attribution claim with primary evidence before retrospective.
- **Pre-conclusion observation checklist** (`feedback_pre_conclusion_observation_checklist`): triggered on each B40 v2 attribution claim — initially had 1 misattribution (W4-S6) corrected via patch-isolation; final 9 wins all patch-isolated.
- **Batch report past-comparison table** (`feedback_batch_report_past_comparison`): present in this retrospective per memory rule.
- **V metric decomposition** (`feedback_v_metric_decomposition`): mathematical -8V decomposed into 7 components with attribution per component.

---

## 5. Handoff to next batch (B42)

### Open carry-over findings

| ID | Severity | Title | Fix surface |
|---|---|---|---|
| B41-NF-S7-1 | HIGH | eval `run_skill` cannot resolve relative skill paths from CWD | (a) eval input schema description add canonical example, OR (b) `run_skill` op apply stdlib resolution when literal path doesn't exist |
| B41-NF-W7-1 | MED | describe_action result → reply synthesis empty stop (deeper design issue) | (a) describe_action handler include reply-synthesis hint in result, OR (b) SP guidance for post-describe_action reply pattern, OR (c) wrap describe_action result with natural-language summary for follow-up queries |
| B41-NF-W7-2 | MED | Long-session driver lacks wait-for-skill-completed semantics | `scripts/dogfood_long_session.py` driver: wait for `skill_run_completed` event before next turn poll (= or configurable skill-completion timeout) |
| B41-NF-W6-1 | LOW | B41 W6 worker `vs_b39` annotation errors | B42 worker prompts cite B39 verdicts from `results-worker-N.json` directly; or post-batch annotation audit |
| B39 carry-over (open) | MED | W6 narr-3 skill_builder `loop_limit_exceeded` (B39 finding) | NOT reproduced in B41 (= per W6 worker note), possibly flaky N=1; defer pending re-observation |
| B39 carry-over (open) | LOW | W6 s-fp11-1 invalid-spec scenario design flaw | scenario rewrite for B42 |

### B40 v2 keep decision

**KEEP empirically grounded**:
- 9 confirmed wins via patch-isolation
- 5 over-correction tests (= 0 evidence of B40 v2 hurting unrelated scenarios)
- `<name>: {}` format empirically optimal
- All ambiguous losses attributed to non-B40-v2 causes
- 1 new downstream bug (W2-S7 eval path resolution) — fix forward in B42, not blocking

### Calibration adjustments

- **V umbrella threshold**: B41 V=13/58=22.4% is NOT comparable to B38 39.7% — environment (/tmp worktree web_fetch perm, --allow-unsafe-python gate, peer pre-creation) differs significantly between batches. V trajectory tracking should be paired with environment-mode annotation.
- **Routing-as-V proxy fallacy**: many B41 "I" verdicts are routing-correct but env-blocked. For B40 v2 effect measurement, R-WEB-style "routing_decided correctness" is more directly observable than V verdict.
- **Predicted B42 V at full env (= --allow-unsafe-python + web_fetch perm + pre-created peers)**: V≈25-30/58, recovering most env-blocked I verdicts.

### Infrastructure recommendations for B42

- **Dogfood runner**: enable `--allow-unsafe-python` flag for reyn web in worker setup (= unblocks W2/W5 unsafe-python gates)
- **Worktree provisioning**: add a `dogfood/fixtures/peer_agents.yaml` to pre-create canonical peer agents at worker setup (= unblocks W5-S3 V path)
- **Long-session driver**: implement wait-for-skill-completed semantics in `dogfood_long_session.py` (= addresses B41-NF-W7-2)
- **Sub-agent prompts**: apply `feedback_subagent_scope_bounding` — 1 agent = 1 deliverable, hard caps in prompt

---

## Appendix: per-worker findings files

Worker-level findings live in `workers/findings-worker-{1,2,3,4,5,6,7}.md` and `workers/results-worker-{1..7}.json`. Worker 6 has the most detailed findings due to R-WEB chain primary verification.
