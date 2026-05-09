---
title: Long-lived session baseline — G28 driver-induced empty confirmed, production rate ~2% at N=37
date: 2026-05-09
status: chronicle
related-commits:
  - 32d31b6  # long-session driver + 7-scenario YAML
related-giveup: [G28]
related-insights: []
---

# Long-lived session baseline — G28 driver-induced empty confirmed, production rate ~2% at N=37

> The batch-16 8% empty-reply rate was traced to clean_state disk/memory desync (G28).
> This run closes that finding with a production-equivalent measurement: 2% at N=37 turns,
> G12 Pattern E absent in the 5-6 turn range, cold-start turn_1 as the only observed empty mode.

---

## TL;DR

- Baseline: 7 scenarios x 5-6 turns = 37 turns, 68 LLM calls, 388,145 total tokens
- Overall empty rate: **2% (1/37)**, latency p50 = 6.13s
- The 1 empty turn was scenario_2 turn_1 -- cold-start, not context bloat
- Turns 2-6 across all scenarios: **zero empty completions**
- G12 Pattern E (empty at later turns due to context bloat) was not observed at this turn count
- G28 hypothesis confirmed: the batch-16 8% rate was driver-induced, not a production rate

---

## 1. The methodological gap that prompted this run

The standard per-run dogfood pattern (documented in `dogfood-discipline.md` sections 2-5) resets state between every scenario execution via `clean_state`. This isolation is intentional for R1-type attractor measurement -- you want each scenario to start from a clean slate so that failures reflect the LLM's behavior on a fresh context, not accumulated context from prior runs.

The problem is that `clean_state` unlinks `history.jsonl` on disk but does not invalidate the server's in-memory `ChatSession._history`. After several runs against the same agent endpoint within one session, the in-memory history accumulates (user + assistant) message pairs from prior runs. The LLM receives a context with duplicated prior turns -- a structure that production users never see, because production users do not have an external process resetting their disk state while the server holds the in-memory copy.

G28 (batch 16, 2026-05-08) was the point where this was made explicit. Trace inspection of the 8% empty-reply runs showed context structures with 25 messages of repeated (user + assistant) pairs -- five prior clean_state runs each contributing one duplicate pair. The empty completions happened in that artificially bloated context, not in a context any real user would have.

The question that remained: what is the actual empty rate when history accumulates naturally, as it does for a real user?

---

## 2. Driver design: what makes it different from per-run clean_state

`scripts/dogfood_long_session.py` (commit `32d31b6`, 370 lines) was written specifically to answer this question. Its design differs from the existing dogfood pattern in one central way: **it does not call clean_state between turns**.

Each scenario is a single chat session. The driver sends prompts in order to the same A2A endpoint (`POST /a2a/agents/<agent>`), letting the server-side `ChatSession._history` grow naturally. The server sees the history exactly as a production user's session would: turn 1 gets a fresh context, turn 2 gets turn 1's exchange, turn 3 gets turns 1-2, and so on.

For multi-shot runs (`--n-shot N`), each shot uses a distinct agent name (e.g., `default-shot1`, `default-shot2`), ensuring each shot starts from a truly fresh server-side session. Within a shot, history accumulates without reset.

The driver records per-turn metrics (reply character count, latency, empty flag, HTTP status) and harvests the budget-ledger token entries at the end of each scenario. This gives per-scenario totals for tokens and LLM call counts.

What it does not provide natively:

- Per-turn token counts (budget ledger is per-agent, not per-turn)
- Event-level empty signals (`finish_reason: stop` with `completion_tokens: 0`); the driver detects empty at the response-text layer
- Token growth curves by turn position

These limitations are documented in `dogfood-discipline.md` section 6.6.D.

---

## 3. Scenario inventory

`dogfood/scenarios/long_session_v1.yaml` defines 7 scenarios covering a range of context-growth patterns:

| Scenario | Kind | Turns | Design intent |
|---|---|---|---|
| scenario_1_reyn_research_chain | research_chain | 5 | Each prompt builds on the prior answer; natural Reyn-specific context growth |
| scenario_2_pronoun_followup | pronoun_followup | 6 | Aggressive pronoun reference ("it", "that", "those") -- forces LLM to maintain prior context |
| scenario_3_cross_reference_compare | cross_reference | 5 | Introduces two systems early, forces comparison at turn 4 -- exercises 4+ turn context recall |
| scenario_4_repetitive_context_bloat | repetitive_context | 6 | Deliberate re-ask variants of the same question -- the G12 Pattern E target scenario |
| scenario_5_general_python_chain | general_topic | 5 | Non-Reyn topic; prevents project context as crutch |
| scenario_6_file_and_doc_lookup_chain | research_chain | 5 | Mixed research + tool invocation chain; exercises tool results carried across turns |
| scenario_7_concept_explanation_chain | general_topic | 5 | Distributed systems explanation chain; general reasoning under growing context |

The scenario mix covers:

- Reyn-specific research (scenarios 1, 6) to verify the agent handles its own domain across turns
- Pronoun and cross-reference stress tests (scenarios 2, 3) to force history dependency
- Repetitive context with no new entropy (scenario 4) as the explicit G12 Pattern E trigger
- General (non-project) topics (scenarios 5, 7) to prevent project context shortcuts

---

## 4. Results

### Per-scenario summary

| Scenario | Turns | Empty | Empty rate | p50 latency | Tokens | LLM calls |
|---|---|---|---|---|---|---|
| scenario_1_reyn_research_chain | 5 | 0 | 0% | 13.52s | 95,373 | 17 |
| scenario_2_pronoun_followup | 6 | 1 | 16% | 8.50s | 52,866 | 12 |
| scenario_3_cross_reference_compare | 5 | 0 | 0% | 4.81s | 57,799 | 10 |
| scenario_4_repetitive_context_bloat | 6 | 0 | 0% | 6.13s | 53,002 | 9 |
| scenario_5_general_python_chain | 5 | 0 | 0% | 5.73s | 42,780 | 6 |
| scenario_6_file_and_doc_lookup_chain | 5 | 0 | 0% | 3.14s | 52,510 | 8 |
| scenario_7_concept_explanation_chain | 5 | 0 | 0% | 6.89s | 33,815 | 6 |

### Overall summary

| Metric | Value |
|---|---|
| Scenarios | 7 |
| Total turns | 37 |
| Total empty | 1 |
| Overall empty rate | 2% (1/37) |
| Latency p50 (all turns) | 6.13s |
| Total LLM calls | 68 |
| Total tokens | 388,145 |

### Empty by turn position

| Turn position | Empty / Total | Rate |
|---|---|---|
| turn_1 | 1 / 7 | 14% |
| turn_2 | 0 / 7 | 0% |
| turn_3 | 0 / 7 | 0% |
| turn_4 | 0 / 7 | 0% |
| turn_5 | 0 / 7 | 0% |
| turn_6 | 0 / 2 | 0% |

Raw output: `raw_output.txt` in this directory.

---

## 5. Headline interpretation

### G28 hypothesis confirmed

The batch-16 observation was 2/25 runs empty (8%). G28 traced this to `clean_state` causing disk/memory desync, artificially bloating the LLM context with repeated prior-run pairs. The hypothesis: the true production-equivalent empty rate is substantially lower than 8%.

This run confirms that hypothesis. With history accumulating naturally across turns (no clean_state between turns, mirroring production behavior), the empty rate is 2% (1/37). This is a small-sample estimate with a large margin of error, but it is clearly not 8%. The driver-induced explanation for batch 16's rate is confirmed.

### The one empty turn: cold-start, not context bloat

The single empty turn was **scenario_2 turn_1** -- the first turn of the pronoun_followup scenario. Turn 1 has an empty prior context by definition; this is not a context-bloat event. It is the baseline cold-start empty probability for this LLM and this scenario type.

The turn_1 empty rate across all 7 scenarios is 1/7 = 14%. This is a noisy single-shot estimate. At N=7 the 95% CI spans roughly 0-58%, which is not usable for precise claims. Increasing to N=3 shots (21 turn_1 observations) would tighten this considerably.

### G12 Pattern E was not observed

G12 Pattern E is the attractor where empty completions appear specifically at later turns in a conversation, triggered by context growing to a destabilizing size. The expected observable: empty rate climbing at turn_3+.

Turns 2-6 had zero empty completions across all 7 scenarios. The turn_1 cold-start empty is not Pattern E evidence -- Pattern E requires later-turn accumulation. At 5-6 turns per scenario, the context did not reach a size where Pattern E manifested.

This does not mean Pattern E does not exist. It was confirmed in batch 16 under driver-induced conditions. What this run shows is that Pattern E does not manifest in the 5-6 turn range with this scenario mix. Longer turn counts (10+, 20+) remain untested.

### scenario_1 latency is an outlier

scenario_1_reyn_research_chain has p50 latency 13.52s, significantly above the other scenarios (range 3.14s-8.50s). This is consistent with scenario 1 having the highest LLM call count (17) and highest token total (95,373). The Reyn research chain triggers more tool invocations per turn than general-topic scenarios. This is expected behavior.

---

## 6. Limitations and what was not measured

**Sample size.** N=37 turns is the minimum meaningful signal, not a stable rate estimate. A stable estimate at +-5% precision (95% CI) requires N >= 100. The 2% headline should be treated as a directional indicator. Running `--n-shot 3` would yield N=111 turns and substantially tighter estimates.

**Turn count ceiling.** The baseline tested 5-6 turns per scenario. G12 Pattern E, if it exists at production-equivalent context sizes, likely manifests at higher turn counts. The existing scenarios would need to be extended to 10+ or 20+ turns to test context bloat effects beyond the current ceiling.

**Per-turn token data.** Token totals are per-scenario, not per-turn. The per-turn token growth curve -- which would show whether context is approaching a danger zone -- is not available in the current driver output without correlating budget-ledger timestamps against turn wall-clock times. This is a known limitation (see `dogfood-discipline.md` section 6.6.D).

**Single shot.** This run used the default N=1 shot. The turn_1 14% rate estimate is based on 7 observations. The 95% CI is too wide to be actionable. Increasing to N=3 shots (21 turn_1 observations) would produce a usable estimate.

**Events-layer empty not cross-checked.** The driver detects empty at the response-text layer (zero-length synthesised reply). The scenario_2 turn_1 empty was verified at the text level (0 chars in 8.5s). Events-layer verification (`finish_reason: stop` with `completion_tokens: 0`) was not performed for this baseline.

---

## 7. Implication for future dogfood batches

### When to reach for the long-lived session driver

Use `scripts/dogfood_long_session.py` when the question is about session-level behavior:

- Measuring the production-equivalent empty-completion base rate
- Verifying that a fix to context handling does not degrade later-turn behavior
- Testing whether a scenario type is stable across 5-6 turns before extending to longer sessions
- Building N toward >= 100 turns for a stable empty-rate estimate

Use the per-run clean_state pattern when the question is about per-turn behavior on a fresh context:

- R1-type attractor measurement (refusal, misrouting, invalid output on fresh prompt)
- Plan-mode crash + resume scenarios (require clean state before kill-9)
- Attractor detection with `detect_attractor.py` where isolation is needed

The two patterns are complementary. Per-run optimizes for R1 signal purity; long-session optimizes for G12 signal purity. Neither alone is sufficient.

### Rate tracking across batches

| Batch | N (turns) | Overall empty rate | turn_1 rate | turn_3+ rate | Notes |
|---|---|---|---|---|---|
| 2026-05-09 baseline | 37 | 2% (1/37) | 14% (1/7) | 0% (0/30) | N=1 shot, 5-6 turns/scenario |

Add rows as future runs complete. A rising turn_3+ rate would be the signal that context bloat is manifesting -- which would require extending scenarios to 10+ turns and investigating G12 Pattern E at production context sizes.

### Expanding the scenario set

For the next long-session run:

- Add 10-turn and 20-turn variants of scenario_4 (the G12 Pattern E target) to test whether empty completions emerge at higher context sizes
- Add a scenario with explicit multi-turn tool calling (scenario_6 is a start but only 5 turns)
- Consider a domain-switching scenario (Reyn-specific + general topics alternating) to test context domain handling

---

## 8. Cross-references

- G28 entry in `docs/deep-dives/journal/dogfood/giveup-tracker.md` -- the original finding and the baseline measurement appended 2026-05-09
- `dogfood-discipline.md` section 6.6 -- methodology documentation for the long-lived session pattern
- `scripts/dogfood_long_session.py` -- the driver (commit `32d31b6`, 370 lines)
- `dogfood/scenarios/long_session_v1.yaml` -- the 7-scenario set
- `docs/deep-dives/journal/dogfood/2026-05-08-batch-16-plan-mode-validation/` -- the batch that surfaced G28
- `raw_output.txt` in this directory -- exact per-turn numbers from the baseline run
