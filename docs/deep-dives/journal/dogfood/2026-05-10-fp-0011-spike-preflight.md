# FP-0011 Spike Preflight — Audit Document

**Date:** 2026-05-10
**Track:** Pre-spike audit infrastructure (parallel track to Track C scenario design)
**Spike branch:** `claude/fp-0011-narrator-removal-spike`
**Script:** `scripts/spike_preflight.py`

---

## Summary

This document records three pre-spike audits required by
`feedback_pre_retrospective_discipline.md`:

1. Trace + measurement pipeline verification (Part 1, via `spike_preflight.py`)
2. Tool description signal-density audit (Part 2)
3. Scenario 4-dimension audit (Part 3 — Track C YAML available)

**Preflight script self-test result:** `WARN` — 4 of 5 checks passed; Check 1
reports a known infrastructure issue with thinking-token suppression (see below).

---

## Part 1 — Preflight Script Results

Run with: `LITELLM_API_BASE=http://localhost:4000 python scripts/spike_preflight.py`

```
================================================================
G4 spike preflight — FP-0011
================================================================
⚠️  Check 1 — strong-tier proxy reachable (pong)
✅ Check 2 — Reyn call_llm works with strong tier
✅ Check 3 — trace dump format works for str + dict model spec
✅ Check 4 — events log has llm_called events
✅ Check 5 — RPD counter sanity

⚠️  Issues:
  ⚠️  Check 1 — strong-tier proxy reachable (pong)
       response='OK'  reasoning_tokens=14
       ⚠️  thinking_tokens=14 (thinkingConfig not disableable via proxy
           — see KNOWN ISSUE below)

Status: WARN — fix issue(s) above before running spike, then re-run preflight.
```

### KNOWN ISSUE — thinking tokens cannot be suppressed via current proxy

**Impact:** HIGH for spike cost estimates; LOW for correctness.

`reyn.local.yaml` declares `strong.extra_body.thinkingConfig.thinkingBudget=0`
to disable Gemini 2.5 Flash's thinking mode. This works in direct LiteLLM
calls but the current proxy setup (`http://localhost:4000`) routes
`gemini-2.5-flash` → `gemini/gemini-2.5-flash` via LiteLLM's native Gemini
provider. When `thinkingConfig` is passed in `extra_body` through the
OpenAI-compat endpoint, the proxy rejects it with HTTP 400
(`Unknown name "thinkingConfig"`).

**Observed thinking usage:** A simple "Reply with: OK" prompt consumed
14 reasoning tokens. For a full skill invocation (20+ LLM turns) this
projects to ~280-560 thinking tokens per run on top of normal text tokens.

**Spike cost revision:**
- Original estimate (`project_g4_spike_cost_estimates.md`): ~$0.13 for
  Mid scenario (3 scenarios × 5 shots)
- Revised estimate with thinking enabled: add ~$0.02-0.05 for thinking tokens
  (gemini-2.5-flash thinking = $2.50/M output tokens, same rate as text)
- Total revised: ~$0.15-0.18

**Operator action before spike:**
Option A (recommended): Accept thinking-enabled spike. The attractor
measurement (narration quality) is not affected by whether the model thinks.
Option B: Configure LiteLLM proxy with `thinkingConfig: {thinkingBudget: 0}`
at the proxy config level (not via extra_body in the request). Requires
restarting the proxy.

### Check 2 — Reyn call_llm path

PASS. The call_llm → LiteLLM → proxy chain works end-to-end for the
strong tier. ModelSpec with dict form resolves correctly.

**Note:** The call was made without thinkingConfig (it would cause 400).
The `gemini-2.5-flash` model without thinkingConfig works and returns
structured JSON as expected by the OS.

### Check 3 — Trace dump format

PASS. Both `str` model spec form and `dict` model spec form produce
parseable JSONL records with:
- `kind`, `request_id`, `timestamp`, `model`, `messages` in request records
- `kind`, `request_id`, `content`, `usage` in response records
- Request/response pairs linked by `request_id`
- `spec_kwargs` field present in trace (= ModelSpec.kwargs visible)

**Note on extra_body in trace:** When the actual spike runs with
thinkingConfig, the spec_kwargs in the trace will show the extra_body dict.
The trace dump correctly captures spec_kwargs from ModelSpec.kwargs — this
was verified with a temperature=0 kwargs field (thinkingConfig is omitted
from preflight to avoid the 400 error).

### Check 4 — Events log llm_called

PASS. EventLog + EventStore round-trip works:
- `ev_log.emit("llm_called", phase=..., model=...)` captured by subscriber
- Written via EventStore to JSONL
- Read back via `iter_all()` with correct type + data fields preserved

The kernel's actual emit path (`runtime.py:798`) is identical to what
preflight exercises. The spike driver can reliably grep events for
`llm_called` with `model=gemini-2.5-flash` to count per-run LLM calls.

**Actual event type is `llm_called`** (not `llm_call_started` as specified
in the task brief). Runtime emits `"llm_called"` with fields `phase` and
`model`. The spike driver should filter on `type == "llm_called"`.

### Check 5 — RPD counter

PASS. `spike_results/fp_0011/rpd_state.json` created fresh at 0/8000.
Operator must increment this manually after each spike run (or wire a
post-run hook). The counter is date-reset-aware (resets on new UTC day).

---

## Part 2 — Tool Description Signal-Density Audit

**Context:** This audit applies `feedback_pre_fix_context_analysis.md`
prospectively to the FP-0011 spike change rather than retrospectively.
The spike adds post-`invoke_skill` narration guidance to the router SP.
The question: are other tools' descriptions stronger narration attractors
that could override this guidance?

### invoke_skill (modified by spike)

**Description (current on spike branch):**
> Run a skill from the registered list. The 'name' parameter MUST be one
> of the skills listed in the system prompt's "Available skills" section,
> used verbatim (no dots, no slashes, no namespace prefixes). Use
> list_skills' input_fields hint to construct the correct input, or call
> describe_skill for full schema details. Do not guess input field names.

**Post-invoke_skill SP guidance (added in spike diff):**

```
- After invoke_skill returns: reply in 1-2 sentences summarising
  what the skill accomplished. Extract the user-relevant fields
  from `data` — do not echo the raw JSON. Status guidance:
    * "finished"             — confirm completion; if applicable, hint at the next step.
    * "loop_limit_exceeded"  — say the skill ran out of phase budget; suggest re-running
      with higher safety.loop.max_phase_visits.
    * other                  — describe what didn't complete; suggest the most likely fix.
```

**Assessment:** The invoke_skill description itself is purely dispatch-
oriented (no narration guidance). The narration obligation is injected via
the Behaviour section of the SP (not inside the tool description), which is
consistent with how other post-tool obligations are expressed.

**Signal strength:** MEDIUM. The guidance is a clear imperative but
positioned among 7+ other Behaviour bullets. Risk: weak LLM scans down
the bullet list and the "After invoke_skill returns" bullet loses salience
among parallel bullets.

### list_skills

**Description:**
> Browse the skill catalogue hierarchically. Pass empty string to see
> top-level categories. Pass a category path to drill in. Returns either
> child categories or items, each with name and one-line description.
> After this returns, narrate the skill names directly to the user in your
> next message — do not stop after listing and do not ask for confirmation
> before naming them.

**Assessment:** list_skills carries an EXPLICIT narration obligation in
its description: "After this returns, narrate the skill names directly to
the user." This is **stronger** than the invoke_skill post-narration
guidance because it lives inside the tool description (high LLM attention
weight for tool-specific instructions) rather than the SP Behaviour section.

**Risk:** If the router calls list_skills then invoke_skill in the same
turn, the LLM may satisfy the list_skills narration obligation and treat
the invoke_skill post-narration as optional / already-done. The spike
measures this as part of narr-2-skill-runner if the router list_skills →
invoke_skills before narrating.

### recall

**Description:**
> Search indexed sources by natural-language query. Returns top-K relevant
> chunks with text + metadata. Use this when the user's question is about
> a topic an indexed source covers... Prefer this over `reyn_src_read` /
> file_read when an indexed source description matches the question's topic
> — semantic search across indexed chunks is more reliable than guessing
> a file path.

**Assessment:** No narration obligation. Pure retrieval description.
Signal density: LOW relative to invoke_skill narration.

### reyn_src_read / reyn_src_list

**Description (reyn_src_read excerpt):**
> Read a text file from Reyn's own repository by an exact repo-root-relative
> path. Use for: (a) reading a specific file... or (b) navigating Reyn's
> source / docs when NO indexed source covers the topic.

**Assessment:** No narration obligation. Source-navigation description.
Signal density: LOW relative to invoke_skill narration.

### read_memory_body

**Description:**
> Fetch the full body of one memory entry. Use only when list_memory's
> description is too vague to answer the user.

**Assessment:** No narration obligation. Has an implicit post-retrieval
expectation (why else would you read it?). Signal density: LOW.

### web_search

**Description:**
> Search the public web with DuckDuckGo and return structured results.
> Standard search operators are supported...

**Assessment:** No narration obligation. Signal density: LOW.

### plan

**Description:**
> Decompose a complex query into 2-7 independent sub-tasks. Use ONLY when
> the query needs multi-source synthesis... The terminal step's text reply
> becomes the user-facing answer; design the last step to synthesise.

**Assessment:** Implicit narration: "terminal step's text reply becomes
the user-facing answer" is a design constraint, not a post-tool obligation.
Signal density: LOW for narration triggering.

### Risk Summary — Narration Attractor Competition

| Tool | Narration obligation | Strength | Location |
|---|---|---|---|
| `list_skills` | Explicit ("narrate skill names") | HIGH | Tool description |
| `invoke_skill` (spike) | Explicit ("reply 1-2 sentences") | MEDIUM | SP Behaviour section |
| `recall` | None | LOW | — |
| `reyn_src_*` | None | LOW | — |
| `read_memory_body` | Implicit | LOW | — |
| `web_search` | None | LOW | — |
| `plan` | Design constraint | LOW | — |

**Key risk:** `list_skills` has a STRONGER narration obligation than the
spike's `invoke_skill` guidance because it's in the tool description
(higher attention weight than SP bullets for most LLM implementations).
If a spike scenario flows list_skills → invoke_skill, the LLM may narrate
after list_skills and consider the obligation discharged, producing
under-narration or no narration after invoke_skill.

**Recommendation:** Spike scenarios should use user messages that name a
skill directly (bypassing list_skills) so the list_skills narration
attractor is not in play. This tests the invoke_skill narration obligation
in isolation.

---

## Part 3 — Scenario 4-Dimension Audit

**Track C YAML availability:** `dogfood/scenarios/fp_0011_narration.yaml` exists
(2 scenarios stubbed; Track C notes "will expand to 5"). The audit below covers
the 2 available scenarios.

**Audit template** (from `feedback_scenario_design_audit_checklist.md`):
1. Data semantic match — does the input data match what the prompt asks for?
2. Tool affordance match — does the expected tool afford the action being tested?
3. Structural source-count requirement — how many sources must the router consult?
4. Rational alternative paths — are there other equally-rational actions the router could take?

---

### Scenario narr-1-skill-builder

**Prompt:** "Create a new skill called 'hello_world' that outputs a greeting message."
**Expected skill:** `skill_builder`
**Expected status:** `finished`
**Judge focus:** `skill_name`, `path`

**Dimension 1 — Data semantic match:**
PASS. The prompt asks for skill creation; `skill_builder` is the correct skill.
"hello_world" is a concrete name that the router can pass as input.

**Dimension 2 — Tool affordance match:**
PASS. `invoke_skill(name="skill_builder", input=...)` directly affords
skill creation. The `skill_builder` skill is in the stdlib catalogue.

**Dimension 3 — Structural source-count requirement:**
MIXED. The router needs:
1. Either list_skills to discover `skill_builder` exists OR direct invoke if
   the name is in the enum. If the name is in the enum, the router can
   invoke directly (1 tool call). If the router list_skills first, that
   triggers the list_skills narration attractor (see Part 2 risk).
**Recommendation:** Verify `skill_builder` appears in the invoke_skill
`name` enum. If so, a direct invoke is expected (1 tool call → narration).
If the router uses list_skills first, the narration assessment must account
for which obligation fires first.

**Dimension 4 — Rational alternative paths:**
LOW RISK. "Create a skill" is unambiguous. The only reasonable alternative
is list_skills → describe_skill → invoke_skill (longer path, same end state).
The judge should accept either path as long as post-invoke_skill narration
occurs.

**Audit verdict:** CONDITIONALLY PASS. Add explicit note in driver: verify
the router invokes `skill_builder` without an intermediate list_skills call
(to isolate the narration attractor). If list_skills fires, record this as
a confound in the batch retrospective.

---

### Scenario narr-2-skill-runner

**Prompt:** "Run the hello_world skill with the topic 'morning coffee'."
**Expected skill:** `skill_runner`
**Expected status:** `finished`
**Judge focus:** `skill_name`, `output_summary`

**Dimension 1 — Data semantic match:**
PARTIAL FAIL. The prompt says "run the hello_world skill" but the expected
skill is `skill_runner` (a meta-skill that runs other skills), NOT
`hello_world` directly. This is a semantic mismatch: the user's literal
phrasing ("run the hello_world skill") maps to `invoke_skill(name="hello_world")`
in router vocabulary, not to `invoke_skill(name="skill_runner", input=...)`.

**Risk:** The router may try to invoke `hello_world` directly (if it's in
the enum) rather than via `skill_runner`. The scenario is testing whether
the router finds `skill_runner` as the correct dispatch path, but the user
prompt does not contain enough signal to prefer `skill_runner` over direct
invocation.

**Recommendation (operator must resolve before spike):**
Option A: Change user prompt to "Use the skill_runner skill to execute
hello_world with topic 'morning coffee'" (removes ambiguity, but overly
prescriptive — may not test organic narration).
Option B: Change expected_skill to `hello_world` (if that skill exists)
— tests direct dispatch narration.
Option C: Add the scenario context that `hello_world` does NOT accept
direct invocation (= no entry in invoke_skill enum), forcing the router
to use `skill_runner`.
Option D: Add a note in judge_focus that either `hello_world` or
`skill_runner` dispatch counts as correct (permissive judge).

**Dimension 2 — Tool affordance match:**
PASS if skill_runner is the expected path. FAIL if the router tries
hello_world directly and skill_runner is the wrong abstraction level for
this test.

**Dimension 3 — Structural source-count requirement:**
HIGH. The router must: (a) find that hello_world needs to run, (b) decide
whether to invoke hello_world directly or via skill_runner, (c) construct
the correct input for skill_runner (= nested skill + topic). This is a
multi-step chain with higher attractor surface.

**Dimension 4 — Rational alternative paths:**
HIGH RISK. Multiple equally-rational paths:
1. invoke_skill(hello_world, {"topic": "morning coffee"}) — direct
2. invoke_skill(skill_runner, {"skill": "hello_world", "topic": "..."}) — via runner
3. list_skills → find hello_world → invoke → narrate (direct, no runner)

**Audit verdict:** FAIL — semantic mismatch between user prompt and
expected_skill. Recommend Option A or C before running spike.
This scenario MUST be redesigned or annotated before the spike starts.

---

### Scenarios Not Yet Present (Track C stub: "will expand to 5")

Track C indicated 5 scenarios total; the YAML contains 2. The 3 missing
scenarios cannot be audited. Operator must apply the 4-dimension checklist
(see below) to each new scenario before the spike begins.

**4-dimension checklist template for operator to apply to scenarios 3-5:**

```
Scenario ID: <id>
Prompt: "<user_prompt>"
Expected skill: <skill>

Dimension 1 — Data semantic match:
  - Does the prompt's topic/action directly map to the expected skill?
  - Is there a word in the prompt that the LLM might map to a DIFFERENT
    skill name? (e.g. "run X" → X directly, not skill_runner)

Dimension 2 — Tool affordance match:
  - Does invoke_skill(expected_skill, ...) directly afford the prompt action?
  - Are there other tools (list_skills, recall, reyn_src_read) that might
    better match the prompt's surface semantics?

Dimension 3 — Structural source-count requirement:
  - Minimum tool calls for correct path: N (1=direct invoke, 2=list+invoke, 3+=multi-hop)
  - If N>1: which tool-call attractor fires first?

Dimension 4 — Rational alternative paths:
  - List every equally-rational alternative the router might take.
  - For each: does the judge accept it as a valid run?
  - Rate: LOW / MEDIUM / HIGH ambiguity
```

---

## Summary for Operator

### Before running spike

1. **Check 1 (WARN):** Decide whether to accept thinking-enabled mode or
   reconfigure proxy. If accepting thinking: revise cost estimate to ~$0.15-0.18.

2. **Scenario narr-2 (FAIL audit):** Fix semantic mismatch in user_prompt or
   expected_skill before spike. Recommended: Option A or C (see above).

3. **Scenarios 3-5:** Apply 4-dimension checklist to new scenarios when
   Track C delivers them.

4. **list_skills narration risk:** In spike analysis, note whether any
   scenario triggered list_skills before invoke_skill. If so, the
   post-invoke_skill narration finding may be confounded by the list_skills
   narration attractor.

### Pipeline status

| Component | Status | Notes |
|---|---|---|
| Proxy reachable | WARN | Thinking cannot be disabled; accept or fix |
| Reyn call_llm path | PASS | Works end-to-end |
| Trace dump | PASS | Both str + dict model spec forms work |
| Events log | PASS | llm_called round-trips correctly |
| RPD counter | PASS | 0/8000 remaining |
| Scenario narr-1 | CONDITIONAL PASS | Watch for list_skills confound |
| Scenario narr-2 | FAIL audit | Redesign before spike |
| Scenarios 3-5 | TODO | Apply 4D checklist when delivered |
