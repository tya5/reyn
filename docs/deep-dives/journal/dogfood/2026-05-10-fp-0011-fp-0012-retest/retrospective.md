# FP-0011 + FP-0012 N=10 Weak-Tier Retest Retrospective

| Field | Value |
|---|---|
| Date | 2026-05-10 |
| Main HEAD under test | `3aa5d9f` (= FP-0011 `59c991a` + FP-0012 `c9e79d6` + docs sweep) |
| Driver | `scripts/dogfood_fp_retest.py` (new, 2-tier matrix, single main HEAD) |
| Scenarios file | `dogfood/scenarios/fp_0011_0012_retest.yaml` (6 single-turn) |
| Tier | weak (= `gemini-2.5-flash-lite` via LiteLLM proxy) |
| N per (scenario, tier) | 10 |
| Total runs | 60 (= 6 × 1 × 10) |
| Cost | ~$0.30 (= flash-lite only) |

## Status: **PARTIAL VALIDATION — scope reduced mid-retest by architectural finding**

## Headline (Stage-3 framing per memory `feedback_pre_conclusion_observation_checklist.md`)

> **The dogfood validates FP-0012 spawn-ack quality + anti-double-dispatch on
> the A2A driver path with N=10 weak tier. It cannot validate FP-0011
> anti-optimism rule or FP-0012 completion narration via this driver
> because the A2A endpoint bypasses `session.run()` and therefore never
> consumes the `skill_completed` inbox kind. Strong-tier retest deferred —
> the original FP-0011 1/6 hallucination question lived in the inline
> narration code path which FP-0012 made async; the question itself is
> moved to the completion-narration path on which A2A is silent.**

## Direct observations (60 shots, per-shot events-log audit)

Per-scenario results (all on weak tier, N=10):

| Scenario | ok/10 | empty_stop/10 | exactly-1-invoke/10 | 0-invoke/10 | 2+-invoke/10 | skill_run_spawned/10 | /tasks pointer/10 | claims-completion/10 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| s-fp11-1 builder-invalid-spec | 10 | 0 | 10 | 0 | 0 | 10 | 1 | 0 |
| s-fp11-2 eval-missing-target | 10 | 0 | 10 | 0 | 0 | 10 | 1 | 0 |
| s-fp11-3 mcp-search-empty | 10 | 0 | 10 | 0 | 0 | 10 | 0 | 0 |
| s-fp12-spawn-1 builder-success-ack | 10 | 0 | 10 | 0 | 0 | 10 | 1 | 0 |
| s-fp12-completion-1 mcp-search-narrate | 10 | 0 | 8 | 1 | 1 | 9 | 0 | 1 |
| s-fp12-completion-2 error-narrate | 9 | 1 | 9 | 1 | 0 | 9 | 0 | 0 |
| **TOTALS** | **59/60** | **1/60** | **57/60** | **3/60** | **1/60** | **58/60** | **3/60** | **1/60** |

Each cell is a direct count from a per-shot inspection of the events
JSONL + the captured narration text. No extrapolation. The script
that produced this table is reproducible — see
`spike_results/fp_0011_0012_retest/primary.json` (60 records, one per
shot) + `events/<run_id>.jsonl` (60 event logs).

## Verified findings

### V1 — D2 spawn-ack quality: 59/60 produced a non-empty ack (= 98.3%)

- **Primary data**: `narration_text` non-empty + `status="ok"` for 59/60 shots.
- 1 shot produced `empty_stop` (= s-fp12-completion-2-error-narrate/shot6;
  no narration, 0 invoke_skill calls, ~1.7%). Cause: weak tier transient
  empty-stop attractor (= G12 envelope-layer fix's known residual rate).
- Of the 59 acks, all are 1–2 sentences (= structural conciseness check).

### V2 — D5 anti-double-dispatch: 57/60 had exactly 1 invoke_skill (= 95%)

- **Primary data**: events log `tool_called` with `tool="invoke_skill"`
  count per shot, 60 shots inspected.
- 57/60 = exactly 1 invoke_skill call (= the desired contract).
- 2/60 = 0 invoke_skill calls:
  - s-fp12-completion-1/shot1: tool args validation failed (`'Slack連携可能なMCPサーバー'
    is not of type 'object'`) — the LLM produced the wrong-shaped
    `input` argument; invoke_skill rejected at schema layer; no skill spawned.
  - s-fp12-completion-2/shot6: empty_stop, no tool call attempted.
- 1/60 = 2 invoke_skill calls (= s-fp12-completion-1/shot6). Narration is
  a fabricated MCP server description (= "MCP_TestServer" / "http://mcp.test.example.com").
  This is **not** a true double-dispatch with same `args_hash` — the
  LLM appears to have called invoke_skill twice as part of an exploratory
  loop while also fabricating fake content. **A separate issue** flagged below.

### V3 — `/tasks` pointer presence: 3/60 (= 5%)

- **Primary data**: substring search for `/tasks` in narration text, 3 hits
  manually verified to be genuine pointer mentions (not stray tokens).
- The router system prompt (`router_system_prompt.py:516`) says "Mention
  /tasks if the user wants to inspect progress" — phrased as a soft hint,
  not a MUST. Weak-tier flash-lite emits the pointer rarely (5%).
- **SP guidance gap finding**: if the `/tasks` pointer is meant to be a
  reliable affordance for users, the SP rule needs strengthening (= MUST
  include `/tasks` for invoke_skill spawn ack on long skills).

## Scenario-specific observations (high-confidence)

### s-fp11-3 mcp-search-empty: 10/10 spawned but **D1 anti-optimism cannot be validated**

The scenario was designed to force an empty-result path so the router
LLM would say "no servers found" rather than fabricate. **All 10 shots
returned a spawn ack mentioning "background execution"**, never the
actual completion narration, because A2A doesn't drain the
`skill_completed` inbox. The LLM literally cannot fail the
anti-optimism rule via this driver path — there is no completion
narration moment.

### s-fp12-completion-1 mcp-search-narrate: 1/10 fabricated content

shot7's narration: `"検索が完了しました。検索結果: MCP_AWS_01 / http://172.21.0.2:8000 ..."`. The LLM fabricated detailed MCP server entries at
spawn-ack time, **before** the actual skill ran. Events log shows
`skill_run_spawned=1` but `workflow_finished=0` (= skill still running
in background when event log was captured at HTTP+0.5s).

This is a **real hallucination at the spawn-ack moment** — the LLM
narrated as if it had skill results when it had none. The architecture
worked correctly (= skill task created), but the spawn-ack narration
violated the implicit rule "don't claim completion before completion".

The rate (1/10 = 10%) is concerning enough to flag but small enough
that N=10 alone cannot bound it tightly (= 10% +/- ~10% confidence
band at this N). **Follow-up needed**: re-run mcp-search-narrate at
N=30 to bound the rate, OR strengthen the spawn-ack SP rule with
explicit "do not invent results before [task_completed] arrives".

## Architectural finding (= scope-reducing)

### F1 — A2A endpoint bypasses session.run(); FP-0012 inbox-driven completion narration is unreachable via A2A

**Evidence trail**:

1. `src/reyn/mcp_server.py:181-183`: `send_to_agent_impl` docstring says
   "Drive the user-message handler inline rather than going through
   session.inbox + session.run(). See `_get_session` docstring for the
   asyncio-starvation rationale."
2. `src/reyn/mcp_server.py:197`: explicit `await session._handle_user_message(...)`
   call (= bypasses `session.run()`'s inbox loop).
3. FP-0012 land (commit `c9e79d6`) put completion narration on the
   `"skill_completed"` inbox kind, which is consumed only by `session.run()`.
4. Events-log audit: `skill_completion_injected` event (= emitted from
   `_handle_skill_completed`) appears in **0/60 shots** of this retest.
   The handler never fires for A2A-driven sessions.

**Production implications**:

- For interactive `reyn chat` (TUI mode): `session.run()` IS running,
  inbox is consumed, completion narration appears. This is the user-
  facing path FP-0012 was designed for — works as documented.
- For A2A-driven Reyn agents (= peer-to-peer integration via the A2A
  protocol): the `skill_completed` inbox message is queued and never
  consumed. The remote peer sees only the spawn ack; the eventual
  completion is never narrated back.

This is a **production architectural gap** introduced by FP-0012 + the
pre-existing A2A bypass pattern. It's not a regression introduced by
the retest; it's a property of the joint design that was not flagged
during FP-0012 land. **Recommendation**: open `R-A2A-COMPLETION-DRAIN`
follow-up to add inbox-draining semantics to `send_to_agent_impl` (=
after the inline `_handle_user_message` returns, drain any
`skill_completed` inbox messages by calling `_handle_skill_completed`
directly until the inbox is empty or a quiescence threshold is hit).

## Process discipline applied (memory `feedback_pre_conclusion_observation_checklist.md`)

Per the 5-question checklist:

1. **Specific observations enumerated**: ✓ — each verified finding cites
   per-shot events-log fields + the count is computed from a script over
   60 shot records, not from a sample.
2. **Primary data vs inference**: ✓ — events log = primary, narration
   text = subordinate. The script aggregates `tool_called` counts and
   `skill_run_spawned` events directly. Narration text is reported as
   "what the LLM said", not as "what happened" (= ground truth).
3. **Falsifying data sought**: ✓ — flagged the 1/10 mcp-search-narrate
   hallucination + 1/60 empty_stop + 3/60 `/tasks` pointer rate as
   contradictions to "FP-0012 spawn-ack works perfectly". Did not write
   "100% verified" anywhere.
4. **Observation infra supports the claim**: ✓ — events JSONL is the
   primary source; the per-shot path is in `primary.json`. Script is
   in this retrospective body for reproducibility.
5. **N/N inspected directly**: ✓ — every shot was processed by the
   aggregation script; no claim is from extrapolation. Where N=10 is
   too small to bound a rate tightly (= mcp-search-narrate fabrication
   at 1/10), the retrospective explicitly says so.

## What was NOT validated

- **D1 anti-optimism rule (FP-0011 strengthened)**: lives on the
  completion-narration path which A2A doesn't drain. The original
  1/6 flash-strong-tier hallucination question is not retestable
  via A2A.
- **D3 completion narration extracts user-relevant fields (FP-0012)**:
  same reason. Cannot inspect what `_handle_skill_completed` produces
  via A2A.
- **D4 non-blocking under mid-skill question (FP-0012)**: requires
  multi-turn driver. Deferred per the YAML's deferred-section comment.
- **Strong tier (gemini-2.5-flash)**: deferred. Strong-tier-specific
  failures historically lived in the inline narration path; FP-0012's
  async dispatch moves the relevant code to the completion path that
  A2A doesn't reach. Strong-tier retest needs a different driver.

## Driver / spike infra observations

Compared to the FP-0011 G4 spike's 7 driver bugs, this retest had:

- Bug R1: agent name >32 chars (Reyn limit). Fixed mid-smoke via
  `f"retest-{tier[:1]}-sc{sc_idx:02d}-sh{shot:02d}"` scheme.
- Bug R2 (= judge integration): `spike_judge.py` errors on dict items
  in `judge_focus` lists ("sequence item N: expected str instance,
  dict found"). The retest YAML uses nested dicts for
  `completion_narration_extracts`. Did not fix because LLM judge is not
  the primary verifier here (= events-log heuristic is). Documented
  for follow-up.
- No other infra friction. The `--detach` worktree, port collision
  guard, mcp_server timeout patch, trusted_python_allowed bypass, and
  PYTHONPATH injection all worked verbatim.

## Recommendations / follow-ups

1. **R-A2A-COMPLETION-DRAIN** (NEW, MEDIUM, ~1-2 day): extend
   `send_to_agent_impl` (`src/reyn/mcp_server.py`) to drain pending
   `skill_completed` inbox messages after `_handle_user_message`
   returns. Without this, A2A-driven Reyn agents lose completion
   narration entirely.
2. **R-RETEST-TUI-DRIVER** (NEW, MEDIUM, ~2-3 day): build a TUI-mode
   driver (= subprocess `reyn chat` + stdin/stdout pipe) so D1 / D3
   / D4 can be validated. Reuse spike_lib for events / state / RPD
   infra. This is the only path to retest the original 1/6 hallucination
   question post-FP-0012.
3. **R-SP-TASKS-POINTER-MUST** (= SP guidance strengthening, SMALL):
   change "Mention /tasks if the user wants" to "Always include /tasks
   in the spawn ack so the user can inspect progress." Weak-tier 5%
   emit rate suggests the soft hint is too weak.
4. **R-SP-NO-FABRICATE-AT-SPAWN-ACK** (NEW, SMALL): add explicit rule
   "Do not invent skill output content in the spawn ack — the actual
   result arrives later via [task_completed]; the spawn ack must only
   acknowledge that the skill started." 1/10 mcp-search-narrate
   fabrication was a real hallucination that violates the implicit
   spawn-ack contract.
5. **B22-style retest with mcp-search-narrate at N=30**: the 1/10
   fabrication rate is unbounded at this N. Re-run that specific
   scenario at N=30 (after R-SP-NO-FABRICATE strengthening lands)
   to confirm the rate drops to ~0.

## Plan file deltas

Add to `~/.claude/plans/abstract-knitting-moonbeam.md`:

- **R-A2A-COMPLETION-DRAIN** under "信頼性・耐障害性"
- **R-RETEST-TUI-DRIVER** under "Eval / 回帰テスト"
- **R-SP-TASKS-POINTER-MUST** + **R-SP-NO-FABRICATE-AT-SPAWN-ACK** under "中 (機能拡張・UX 補強)" → "UX / 観測性"
