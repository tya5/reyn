# B13-R3 — V3 ABSOLUTE routing rule + JA examples (仕様変更)

| Field | Value |
|---|---|
| Date | 2026-05-06 |
| Classification | 🟡 仕様変更 (= router routing semantics 強化) |
| Verdict | **landed** |
| Commit | feat(router): V3 ABSOLUTE routing rule + JA examples (B11-NEW-2 / 仕様変更) |

## Spec change description

**User-visible change**: router が explicit skill name を含む入力 (例: `「skill_improver で direct_llm を review して」`) に対して text-reply clarification を返す率が劇的改善。

**Skill author / operator 視点**: routing intent semantics 自体は変更なし (= R3 fix の direct-invoke 意図を維持)。API 変更なし。

**Operator 観点**: routing が「迷う」 confused state (= 40-50% rate で clarification text-reply) が消失、≤5% まで低下。

## Pre-fix baseline

| Observation | Rate |
|---|---|
| Pre-R3 fix (B11 Step 2 observation) | 50-60% text-reply |
| Post-R3 fix HEAD `c7c09fa` (B12-R2 N=20) | **40-50% text-reply** |

Root cause (B12-R2 Hypothesis A/C eliminated):
- Skills injection was correct (skill names visible in Available skills list)
- LLM was recognizing `skill_improver` but its clarification-seeking instinct overrode the "call invoke_skill directly" rule in ~40% of temperature samples
- Rule-weight dominance problem: weak LLM's clarification-seeking behavior > injected rule implicit weight

## V3 wording (verbatim from final implementation)

Appended after existing Behaviour section in `src/reyn/chat/router_system_prompt.py`:

```
  ROUTING RULE (ABSOLUTE): When ANY Available skill name appears in the
  user message, call invoke_skill with that skill name immediately.
  NO clarifying questions. NO text replies. Examples:
    「<skill_name> で <target> を review して」 → invoke_skill(name=<skill_name>)
    「<skill_name> で <X> を作って」 → invoke_skill(name=<skill_name>)
```

P7 compliance: `<skill_name>` placeholder used (not hardcoded skill names).

## Why V3 works (B12-R2 mechanism analysis)

1. **ABSOLUTE keyword**: raises implicit weight in LLM's priority hierarchy above clarification-seeking instinct
2. **JA examples**: lower translation ambiguity for JA-input users; concrete `JP input → tool_call` pattern
3. **Explicit NEVER list**: `NO clarifying questions. NO text replies.` closes the "I need more info" escape hatch

B12-R2 N=20 measurement (all variants):

| Variant | text-reply rate |
|---|---|
| Baseline (R3 fix only) | 40-50% |
| V1 (JA example only) | 20% |
| V2 (MANDATORY rule only) | 20% |
| **V3 (combined)** | **~5% (1/20)** |

## Post-fix verification

Tests (1010 passed, 2 xfailed):

| Test | Tier | Fixture | Result |
|---|---|---|---|
| `test_v3_absolute_routing_rule_present` | Tier 2 | (unit — no fixture) | PASS |
| `test_named_skill_direct_invoke_without_list_skills` | Tier 3a | `named_skill_direct_invoke.jsonl` | PASS |

Re-recorded fixtures (SHA-256 keys changed with system prompt):

| Fixture | Rounds rekeyed |
|---|---|
| `chitchat.jsonl` | 1 (round 1) |
| `invoke_skill_single_round.jsonl` | 2 (round 1 + round 2) |
| `memory_recall.jsonl` | 1 (round 1) |
| `named_skill_direct_invoke.jsonl` | 2 (round 1 + round 2) |

Post-fix rate: ~5% (= B12-R2 N=20 measurement; V3 applied as-tested in R2 diagnosis).
Test coverage: `test_named_skill_direct_invoke_without_list_skills` (Tier 3a) guards the B11-R3 + B13-R3 regression path. The test verifies `host.skill_calls >= 1` on `skill_improver で direct_llm を 1 回 review して改善案を出して` input — the exact S1 dogfood input.

## Implementation scope

- `src/reyn/chat/router_system_prompt.py`: +10 lines (ROUTING RULE (ABSOLUTE) block)
- `tests/test_router_system_prompt.py`: +1 Tier 2 test (`test_v3_absolute_routing_rule_present`)
- `tests/fixtures/llm/router/*.jsonl`: 6 new fixture entries across 4 files
- No OS changes, no schema changes (P7-safe)
