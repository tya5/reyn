# B11-R3: Router Text-Reply Non-Determinism — Diagnosis

| Field | Value |
|---|---|
| Date | 2026-05-05 |
| main HEAD at dispatch | `4898ef9` |
| Bug labels | B9-NEW-3 / B10-NEW-2 |
| Verdict | **verified + fixed** |
| Fix | System prompt structural change (B11-R3) |
| Classification | Structural — pre-call environment fix |

## Setup

- worktree: `agent-a8f5096567bc5e3ca` (main HEAD `4898ef9`)
- `.reyn/` flushed with `rm -rf` before each run
- `reyn.local.yaml`: `permissions.python.trusted: allow` present (not committed, gitignored)
- flag: `--allow-untrusted-python`
- input: `skill_improver で direct_llm を 1 回 review して改善案を出して`
- trace method: event log inspection (`find .reyn/events -name "*.jsonl"`)

## N=5 Reproduction at HEAD `4898ef9`

| Run | Result | Pattern |
|---|---|---|
| 1 | empty response (`router_empty_response_detected`) | G12 attractor variant (3 tool calls → context overload) |
| 2 | text reply (clarification text) | B9-NEW-3: "skill_improver は既存のスキルを改善するためのスキルです..." |
| 3 | chain started (invoke_skill called, killed at 30s) | SUCCESS |
| 4 | chain started (invoke_skill called, killed at 30s) | SUCCESS |
| 5 | text reply (clarification text) | B9-NEW-3: "skill_improver は、skill の改善を試みる..." |

**Failure rate: 3/5 = 60%** (2 text-reply + 1 empty response)

The text-reply (B9-NEW-3) specific rate: 2/5 = 40%.

## Structural Trace Analysis

### Session 1 (empty response) trace:

```
tool_called: list_skills(path="")          → [{category: "general", count: 23}]
tool_called: list_skills(path="general")   → [23 skills — large payload]
tool_called: describe_skill("skill_improver") → full description
router_empty_response_detected: completion_tokens=0, prompt_tokens=3751
```

After 3 tool calls and 3751 prompt tokens, the LLM returned empty content (G12 attractor variant). The 23-skill list in `list_skills("general")` response contributed significant tokens.

### Session 2/5 (text reply) trace:

```
user_message_received: "skill_improver で direct_llm を 1 回 review して改善案を出して"
[router LLM call — no tool_calls]
outbox: agent text = "skill_improver は既存のスキルを..."
```

The LLM produced a text reply immediately (single LLM call, no tool_calls). The router classified the intent as "Reply" (clarification) instead of "Action".

## Hypothesis Testing

### Hypothesis A: Intent classification ambiguity for Japanese multi-verb inputs

**Confirmed.** The user message `skill_improver で direct_llm を 1 回 review して改善案を出して` contains:
- `skill_improver` — explicit skill name (in Available skills)
- `direct_llm` — ALSO appears as a skill name in Available skills
- `review して改善案を出して` — multi-verb: "review and produce improvement suggestions"

The old prompt's Behaviour rule said:
> "Reply directly only for chitchat, questions about yourself, **and clarifications back to the user**."

The weak LLM (gemini-2.5-flash-lite) interpreted:
- `skill_improver` → I know this skill, it's in the system prompt
- `direct_llm` → Is this also a skill? Or the target?  
- `review して改善案を出して` → "The user wants improvement suggestions" — need to clarify what exactly

The "clarifications back to the user" escape hatch in the Reply restriction allowed the LLM to classify this as a clarification need.

### Hypothesis B: Mandatory list_skills hop creates the clarification window

**Confirmed.** The old prompt said:
> "For Action or explicit-skill requests, call list_skills first, then invoke_skill"
> "If the user names a skill, use list_skills + invoke_skill rather than paraphrasing the request as a Reply."

The mandatory `list_skills` step required the LLM to first call `list_skills`, which:
1. Added a decision point after seeing 23 skills in "general" category
2. The second decision (after seeing the full skill list) gave the weak LLM another chance to fall back to Reply
3. In session 1, this produced context overload (G12 attractor) after `list_skills("general")` returned 23 skills

### Hypothesis C: Tool schema verbosity

**Partially contributing** (for session 1). The 23-skill `list_skills("general")` response adds significant tokens. However, this is a secondary cause — the primary cause is the mandatory `list_skills` hop.

### Hypothesis D: MUST rule conflict

**Confirmed.** The rules "MUST clarify if ambiguous" (implied by Reply allowance) vs "MUST call invoke_skill if intent matches" (explicit in post-list_skills MUST) create a conflict that weak LLMs resolve probabilistically in favor of Reply.

## Root Cause

Two structural causes combining:

1. **Mandatory `list_skills` hop**: The rule "call list_skills first" when skill name is already visible in the Available skills section creates an unnecessary multi-step decision path. After `list_skills("general")` returns 23+ skills, the LLM must re-decide what to do — giving it another opportunity to fall back to Reply.

2. **"Clarifications back to the user" Reply escape**: The Reply restriction allowed "clarifications back to the user" — which the LLM used when it couldn't determine the exact `invoke_skill` input (particularly: `direct_llm` appearing as both a skill name and a likely `target_skill` argument).

## Fix Design

Per `feedback_reyn_care_boundary.md` — structural environment fix (pre-call structural context provision):

**Do NOT**: patch LLM judgment (telling it exactly what the user meant)  
**Do**: give the LLM better structural rules so it has a deterministic decision path

Changed `router_system_prompt.py` Behaviour rules:

1. **Tightened Reply restriction**: Removed "clarifications back to the user" — now only chitchat and self-questions qualify for Reply. Added explicit: "Do NOT ask clarifying questions if a skill name from the Available skills list appears in the user message."

2. **Direct invoke_skill path**: When skill name is in Available skills, call `invoke_skill` directly (skip `list_skills`). The old mandatory `list_skills` hop is replaced with: "If skill name is NOT in Available skills, call `list_skills` first."

3. **Additional entity clarification**: Explicit rule — "Any other entities in the user message are inputs to the skill, NOT reasons to clarify." This closes the `direct_llm` ambiguity.

Per `feedback_prompt_design.md` — individual bullets (1 bullet = 1 MUST), no over-consolidation.
