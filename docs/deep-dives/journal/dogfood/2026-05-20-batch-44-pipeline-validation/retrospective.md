# B44 Retrospective — 2026-05-20

**Batch focus**: validate cumulative effect of PR #287 (chat-router empty-stop
retry plumbing) + PR #290 (spawn-ack → LLM via role=tool) under live dogfood
load, and exercise the new B44 batch tooling (`dogfood_batch_dispatch.py` /
`dogfood_aggregate.py`) end-to-end.

- HEAD at dispatch: `bffe32e4`
- ENV: `REYN_EMPTY_STOP_RETRY=1`, `REYN_SPAWN_ACK_TO_LLM=1`
- User params (held constant vs B43): `hot_list_n=10`, `models.tier=flash-lite`
- Hard caps observed: 50 tool-uses / 15 min wall-clock per worker

## Verdict totals

| Metric        | B42  | B43  | **B44**  | ΔvsB43 |
|---------------|------|------|----------|--------|
| V (verified)  | 21   | 22   | **23**   | **+1** |
| I (indirect)  | —    | —    | 8        |        |
| R (refuted)   | —    | —    | 19       |        |
| V/N           | 21/54| 22/54| 23/50    |        |
| Verified rate | 0.39 | 0.41 | **0.46** | +0.05  |

`scenarios_total` differs (54 vs 50) because W6 plan-trio ran 3 scenarios in
B44 (per scenario-set yaml) vs 7-equivalent in prior batches.

## Per-worker (V) — past-comparison table

| Worker | Scenario set                | B43 V | B42 V | **B44 V** | ΔvsB43 |
|--------|-----------------------------|-------|-------|-----------|--------|
| W1     | chat_router_smoke           | 3/7   | 3/7   | 2/7       | -1     |
| W2     | stdlib_skills_core          | 5/9   | 5/9   | 3/9       | -2     |
| W3     | control_ir_ops              | 4/9   | 5/9   | 3/9       | -1     |
| W4     | permissions_and_safety      | 4/8   | 6/8   | **7/8**   | **+3** |
| W5     | multi_agent_and_mcp         | 2/7   | 0/7   | 2/7       | 0      |
| W6     | plan_mode_fp_0011_mixed     | 1/7   | 0/7   | 1/3       | 0¹     |
| W7     | long_session_v1             | 3/7   | 2/7   | **5/7**   | **+2** |

¹ W6 ran 3 scenarios (per yaml) not 7. s2 went R→I = PR #287 unblock at
classification level even though verdict count stayed flat.

## Primary verify — PR #287 (chat-router empty-stop retry)

Observed via W6 events (direct `events.jsonl` grep):

- `router_empty_response_retry_injected` event fired **3×** across 2/3 W6
  scenarios (s1=2, s2=1, s3=0).
- B43 W6 s2 (`plan_explain_with_code_references`) was R-by-empty-stop. B44 s2
  saw 1 retry injection AND completed the full plan lifecycle AND moved to I
  (= no longer empty-stop refuted).
- **Conclusion**: scope-gap fix verified end-to-end. PR #265 (planner sub_loop
  sites only) → PR #287 (extended to top-level chat-router) closes the
  W6-S2 NF.

## Primary verify — PR #290 (spawn-ack → LLM, env-gated)

Observed via W7 events + content scan (35 turns total):

- `invoke_skill_spawn_ack_exit` fired **3×** (s2 ×1, s5 ×2).
- Literal `_SPAWN_ACK_MSG` echoes (`"is running in the background"` /
  `"バックグラウンドで実行"` / `"Use /tasks to monitor"`): **0/35 turns**.
- S7 T5 (B43 10/10 leak attractor) → 4145-char substantive reply, no leak.
- LLM-composed acks observed in s5: `"I've started a process to explain
  Python's asyncio event loop. I'll let you know once it's complete."` (=
  natural-language, references user's actual request, H3 hallucination defense
  holds — no fabricated content).
- **Conclusion**: env-gated `role=tool` + `_SPAWN_ACK_TOOL_DIRECTIVE` path
  validated. NF-W7-B43-2 closed.

## Regressions to investigate (B44 NFs)

- **W2 stdlib_skills_core -2V** (5/9 → 3/9). Reclassification-driven per W2
  worker note; need to read worker findings to determine whether structural
  vs noise.
- **W3 control_ir_ops -1V** (4/9 → 3/9). Within N=1 noise band on flash-lite
  but flagged for follow-up.
- **W1 chat_router_smoke -1V** (3/7 → 2/7). To audit alongside W7's S1 T5
  finding (= honest refusal vs P7-principles answer) — possible
  prompt-affordance mismatch on Reyn-architecture questions.

These are candidates for B45 NF dispatch; no blocking signal for PR #287 /
PR #290 (both have direct primary-verify evidence above).

## Pipeline / tooling debrief

This batch was the first run of `dogfood_batch_dispatch.py` +
`dogfood_aggregate.py` end-to-end. Observations:

- Worker prompt generation from YAML config saved per-worker hand-editing
  (~7× prompt template fanout = ~75% reduction vs B43 hand-pasted prompts).
- `--setup-worktrees` correctly bound flash-lite via reyn.local.yaml override
  for all 7 workers (no strong-model invocation events).
- YAML date field parsed as a `date` object not `str` → aggregate.json
  serialization crash; trivial fix (quote the YAML value). Pre-existing
  edge case in `_normalise_verdicts` would not have caught this. Logged for
  the next aggregator iteration.
- Worker output path inconsistency: W2/W3/W7 wrote `results-worker-N.json`
  to the worktree-relative `journal_dir`, not the main repo. Required a
  manual `cp` step. Candidate doc note for `dogfood-tooling.md`: workers
  must resolve `journal_dir` against the main repo path, not their CWD.

## Carry-overs to B45

- W2 / W3 regression triage (above).
- W7 S1 T5 prompt-affordance NF (B44-NF-W7-1) — scenario design, not a fix.
- `dogfood_batch_dispatch.py` enhancement: emit absolute `journal_dir` into
  worker prompts so worktrees don't drift.
- **PR #290 default-flip plan**: keep `REYN_SPAWN_ACK_TO_LLM=1` opt-in
  through B45–B47, accumulate spawn-ack-path turns to N≥100 with zero
  literal echo + zero H3 re-emergence, then ship a separate PR that flips
  the default (legacy outbox-push path retained for 1 release as
  deprecation window). B44 N=35 (W7 only) is too thin to justify a
  default flip in this PR.
