# ADR-0021: G12 attractor — structural fix design options

**Status**: Accepted (Option F + Option G) — detect + explicit failure UX, no auto-rescue; root cause confirmed, description truncation fix wave in progress (2026-05-04 — root cause confirmed, Option F + truncation fix wave)
**Track**: G12 (giveup-tracker.md) — attractor variant family

## Context

G12 is the long-running attractor issue in which `gemini-2.5-flash-lite` (the
default `light` / `standard` model in the LiteLLM proxy) terminates a router
loop turn with `finish_reason=stop, completion_tokens=0` and no tool calls,
despite the system prompt containing explicit MUST rules such as:

> "After list_skills reveals at least one matching skill, you MUST call
> describe_skill or invoke_skill. Do NOT reply directly."

The issue has recurred across four dogfood batches (B2-H1, B3-H1, B5R2-H1,
B6-S2) in the same `list_skills → describe_skill → stop` sequence. Each
recurrence was met with an additional prompt rule; the rules accumulated but
the attractor persisted. B7-RETRO-H4 (2026-05-04) closed the loop on the
root cause: the MUST rule *was* injected into the live payload at the time of
the empty-stop response. The LLM saw the rule and did not honour it.

### Observed mechanism (RETRO-H4)

```
[T+0.0s] router  list_skills("")         → finish=tool_calls  (1 call)
[T+1.3s] router  list_skills("general")  → finish=tool_calls  (1 call)
[T+2.3s] router  (no tool call)          → finish=stop, completion_tokens=0
```

- `tool_choice: auto` was in effect (confirmed via `llm-detail` inspect).
- The context at T+2.3s included the full skill list (10 skills, `direct_llm`
  visible) and the system prompt MUST rules unchanged from prior commits.
- `completion_tokens=0` indicates the model generated zero output tokens —
  not a truncation at the model context window, but a degenerate API response
  (possible internal truncation mid-generation or a provider-level policy
  cut-off on the Gemini side).

### Router behaviour on empty-stop

`RouterLoop.run()` at line 281–287 (`router_loop.py`) treats an empty-stop
response as a "text reply" and calls `host.put_outbox(kind="agent", text="")`.
The user receives a blank reply; the conversation ends without invoking any
skill. There is no retry, no detection, and no escalation path in the current
code.

### Prior design decision: OS-layer state machine withdrawn

The original B5R2-H1 option set included an OS-layer state machine (track
`list_skills` / `describe_skill` state; gate on `invoke_skill` before
allowing exit). This was withdrawn from G12 on the grounds that:

1. It encodes skill-specific tool names (`invoke_skill`) in OS code — P7
   violation.
2. Each new attractor variant would require a new OS gate — linear bloat.
3. It obscures the G4 trigger signal (weak LLM capacity ceiling).
4. Structurally identical to the prompt bloat trap, only in code.

The withdrawal was recorded in `giveup-tracker.md` G12 "Out-of-scope" and the
G4 spike was placed as the primary fix path, blocked on proxy setup.

### New observational data available (batch 7)

Batch 7 delivered the trace / replay / attractor-detection infra:

- `REYN_LLM_TRACE_DUMP` + `dogfood_trace.py` for payload inspection
- `detect_attractor.py` with `stop_with_must_rule` heuristic (Heuristic 1)
  that precisely identifies the G12 variant from a JSONL trace
- `llm_replay.py` for deterministic re-runs with `--model` swap

These tools change the design space: several options that previously required
"fire and hope" can now be evaluated quantitatively with < 1 day of effort.
The G12 empty-stop frequency was measured at 5/10 (50%) on a fixed payload
(`B7-G12-empty-stop-frequency.md`), confirming the probabilistic nature of
the attractor and directly informing the Option B vs Option F trade-off.

### Root cause confirmed (batch 7 late-stage N-shot experiments)

Two further finding documents closed the loop on the causal mechanism:

- `B7-G12-context-root-cause.md` (a62a9dad): 4-hypothesis N-shot test confirmed
  context verbosity (skill_improver 218-char description) is the decisive
  trigger; MUST rules have zero causal effect.
- `B7-G12-cross-attractor-pattern.md` (a947255e): two trigger paths confirmed
  (list_skills tool_response, system prompt inline embedding) — both must be
  fixed for full rescue.

The H-a experiment (removing MUST rules) showed zero effect on attractor rate,
definitively closing the "MUST rule non-honour" hypothesis that had driven four
batches of prompt iteration. The H-b experiment (shrinking skill catalogue from
1342 to 285 chars) produced a 100% → 0% empty-stop rate. H-b1 (shrinking only
`skill_improver`'s description from 218 chars to under 80) produced 0/5 (0%)
empty-stop — identifying the 218-char description as the decisive trigger.

This changes the causal frame: the attractor is not a model-capability ceiling
but a structural context verbosity problem in Reyn's router environment. It
sits within Reyn's care boundary (pre-call structural integrity), not the LLM's
responsibility.

## Considered options

### Option A — G4 spike (strong model substitution)

Replace `gemini-2.5-flash-lite` with a reasoning-capable model
(`claude-sonnet-4-x` or `gemini-2.5-pro`) for the router path and measure
attractor rate.

**Feasibility**: Blocked. The LiteLLM proxy at `http://localhost:4000` serves
only `codex-proxy` (self-referential loop) and `gemini-2.5-flash-lite`.
Neither `claude-sonnet` nor `gemini-2.5-pro` is registered. Prerequisite:
`/Users/yasudatetsuya/Workspace/junk/litellm/config.yaml` update + proxy
reload. See `g4-trigger-evaluation-spike.md` for the exact YAML snippets.

**Effect hypothesis**: Strong models are unlikely to emit `completion_tokens=0`
on a simple skill-routing task. Evidence from other providers supports this,
but it is a hypothesis — not yet observed for this specific prompt structure.
The `llm_replay.py --model` flag would let us test without a live dogfood run
once the proxy has the model.

**Cost**: Router call cost scales with model capability. `claude-sonnet`
pricing is roughly 20–60x `gemini-2.5-flash-lite` at typical token volumes;
`gemini-2.5-pro` is 10–30x. For the router path (≈ 1 500–2 000 tokens/turn,
2–4 turns/request), estimated cost delta is $0.02–$0.10/request vs < $0.001
today.

**Vision alignment**: The Reyn vision (memory `project_reyn_vision.md`) is
"predictability via constrained reasoning, not model autonomy." Strong models
reduce prompt-engineering overhead but increase cost and reduce
predictability-per-dollar. G4 explicitly defers this trade-off to a
per-customer or per-scenario selector; it is not a default-flip.

**Design surface**: Minimal — `reyn.local.yaml` `models.standard` or a
per-scenario model selector. No OS code change.

**Effort**: 0.5 day (proxy setup + 5-run spike measurement).
**Effect**: High (attractor expected to disappear or drop to < 20% rate).
**ROI**: High if proxy setup cost is borne. **Blocked** until proxy is ready.

---

### Option B — OS-layer attractor detection + retry

Detect the `finish_reason=stop, completion_tokens=0` signature in the router
loop and retry the turn with an augmented prompt.

**Mechanism**: After each `call_llm_tools` in `RouterLoop.run()`, check
whether the response matches the `_is_empty_response` predicate from
`detect_attractor.py` (the same three-line check already implemented there).
On match, inject a fallback instruction into the messages and retry once.

**P-number analysis**:

- P3: The OS is the "runtime engine — context build, LLM call, validation,
  Control IR execution, transitions, events." A retry on a malformed (empty)
  LLM response sits within the runtime engine's existing scope. The current
  `_run_decide_with_retry` in `OSRuntime` already retries on validation
  failures (ValueError). A parallel retry path in the router for structural
  response failures is architecturally consistent.
- P7: The trigger condition is `finish_reason=stop AND completion_tokens=0`
  — a provider-level API property, not a skill-specific string. The retry
  message would need careful wording to avoid embedding skill-concept terms.
  If the retry injection says "you must call a tool" (generic), it is
  P7-clean; if it says "you must call invoke_skill" (skill-specific), it is
  a P7 violation. The design must stay at the generic level.
- P4: The retry does not choose a next phase or artifact — it only forces
  another LLM iteration within the existing router turn. P4 is not violated.

**Precondition**: None. The `_is_empty_response` logic exists in
`detect_attractor.py` and can be inlined or imported.

**Limitation**: Retry rescues the transient-failure case (model glitch) but
cannot rescue the true attractor (where the model will also emit an empty
response on the retry). RETRO-H4 observed only one attractor call per
session; a single retry would have rescued that instance. But if the attractor
is deterministic for a given context (same model, same token sequence), the
retry will also fail. No quantitative data on the retry-success rate exists;
measuring it would require the `llm_replay.py --n` flag.

**Effect on G4 signal**: A retry layer that silently absorbs attractor events
reduces the observable signal needed to justify the G4 spike. This is a
meaningful cost: the attractor evidence is what motivates proxy setup and
strong-model evaluation. Option B should not suppress trace events; any retry
should emit a named event (e.g. `router_attractor_retry`) so the detection
infra continues to surface the issue.

**Effort**: 1 day (implementation + Tier 2 test for the retry path + event
emission + non-regression on normal tool-call paths).
**Effect**: Medium (rescues transient attractor cases; does not fix
deterministic ones).
**ROI**: Medium. Worth doing only if it emits an observable event and does
not suppress the G4 trigger signal.

---

### Option C — Hybrid (weak model + attractor-triggered strong-model retry)

Combine Options A and B: first call with the weak model; if
`detect_attractor.py` Heuristic 1 fires, retry the same turn with the strong
model.

**Mechanism**: `RouterLoop.run()` calls weak model first. On empty-stop, call
`call_llm_tools` again with `model=host.resolve_model("strong")`. The strong
model's result replaces the weak result for the rest of the turn.

**Cost**: Attractor-adaptive. If attractor rate is 30% (B6-S2 implied 4/4 for
that specific scenario), 30% of turns incur a dual-model cost. For
general-purpose usage the attractor scenario is a subset; overall cost delta
is smaller than a flat strong-model switch (Option A).

**Precondition**: Proxy must expose at least one strong model (same blocker as
Option A). Without the proxy, Option C degrades to Option B (retry with the
same model).

**Vision alignment**: Better than Option A for the vision, because the default
path stays on the weak model and cost scales with observed attractor rate
rather than being charged on every turn.

**Complexity**: Higher than A or B individually. The `resolve_model("strong")`
call requires `strong` to be a named slot in `reyn.yaml` `models:`, which is
already part of the config schema (`models.strong` exists). No new config
schema required.

**Effort**: 1.5 days (builds on Option B's retry path, adds model parameter
switching + Tier 3 replay test for the escalation path).
**Effect**: High for transient cases; strong model handles the deterministic
attractor context if the model capability gap is sufficient.
**ROI**: High if proxy is available; blocked otherwise (same as A).

---

### Option D — Provider-native tool_choice enforcement

Set `tool_choice="required"` for the turn immediately following a
`list_skills` or `describe_skill` tool result, forcing the model to call at
least one tool.

**Mechanism**: RETRO-H4 confirmed that `tool_choice="auto"` was active at the
attractor turn. Switching to `tool_choice="required"` for that specific turn
would force a tool call. The LiteLLM `call_llm_tools` signature already
accepts `tool_choice` as a parameter (line 540, `llm.py`); `router_loop.py`
hardcodes `"auto"` at line 180.

**P7 analysis**: The trigger condition ("the previous turn had a tool result
from a specific tool") requires tracking which tools were called in the prior
turn. If this tracking uses the tool name `list_skills` or `describe_skill` as
a literal, it is a P7 violation (skill-specific strings in OS code). A P7-safe
variant would switch to `tool_choice="required"` on *any* turn where the
messages contain one or more tool results (i.e. the model has already made at
least one tool call). This is a structural property of the message history,
not a skill concept.

**Side effect**: `tool_choice="required"` forces the model to emit *some*
tool call, but does not constrain *which* tool. After `list_skills`, the model
could call `list_skills` again (looping), call `describe_skill` (correct), or
call any other available tool. The attractor scenario would be transformed
from "stop without action" to "call a tool, possibly the wrong one." This may
produce different failure modes (e.g. hallucinated tool arguments, infinite
list_skills loops) that are harder to detect and recover from.

**Provider compatibility**: `tool_choice="required"` is supported by
OpenAI-format APIs. Gemini via LiteLLM passes it through; RETRO-H4 confirms
the proxy forwards standard OpenAI params. However, Gemini-specific behaviour
under `tool_choice="required"` with zero generation budget is not documented
— it is possible the provider ignores it or applies it inconsistently on the
same model version that produces `completion_tokens=0`.

**Precondition**: None for implementation. Behavioural verification requires a
live run or `llm_replay.py --patch` to swap `tool_choice`.

**Effort**: 0.5 day (router_loop.py change + conditional logic + Tier 2 test
for the tool_choice switching path).
**Effect**: Low to medium. Eliminates the silent-stop attractor but may
introduce loop or wrong-tool attractors. Net effect is uncertain without
measurement.
**ROI**: Low. High implementation risk due to unknown provider behaviour under
`required`; new failure modes may exceed the cost of the attractor being
replaced.

---

### Option E — Per-session auto-resume after router attractor

When the router emits a blank reply (attractor stop), treat the session as
"incomplete" and re-invoke the router with the original user message in a
fresh turn, integrating with the skill-resume machinery from
[ADR-0012](0012-auto-resume-default.md) and [ADR-0013](0013-exception-aware-crash-lifecycle.md).

**Mechanism**: On empty-stop in `RouterLoop.run()`, emit a named event
(`router_attractor_detected`) and re-queue the original user message as a new
`pending_chain` entry, allowing the session's normal chain-dispatch path to
re-invoke `RouterLoop` on the next iteration.

**Semantic issue**: A fresh RouterLoop invocation on the same user message
with the same model and the same system prompt will likely reproduce the
attractor. The session history would show the prior empty reply; the model
might anchor on it or generate a text reply acknowledging nothing happened.
Neither outcome advances the user's task.

**Integration complexity**: The skill-resume machinery (D-track, ADR-0012/13)
is phase-level, not router-turn-level. Wiring the router's attractor exit into
the D-track resume loop would require extending `SkillResumeAnalyzer` and
`ChatSession._auto_resume_active_skills` to cover a new "router attractor"
lifecycle state. This is significant scope for a path with low expected
success rate.

**Precondition**: None for the infrastructure, but the option is only
meaningful in combination with Option A or C (different model on retry). As a
standalone retry-same-model option it is dominated by Option B with less
complexity.

**Effort**: 3+ days (new lifecycle state, event taxonomy, ChatSession wiring,
D-track extension).
**Effect**: Low as standalone. Only meaningful as an async variant of Option C.
**ROI**: Low. Option B covers the synchronous retry case with far less
complexity; Option E adds an async restart path that is unlikely to resolve
the deterministic attractor without a model change.

---

### Option G — Skill description truncation (structural fix at root cause) — ADOPTED 2026-05-04

**Mechanism**: Truncate skill description to ≤80 characters in two locations:
- `list_skills` tool_response builder (`router_loop.py`)
- system prompt inline skill list (`router_system_prompt.py`)

`describe_skill` continues to return full description (summary vs detail
pattern, RFC-7807-like).

**P-number analysis**:
- P3: structural environment shaping, OS responsibility (router build) ✅
- P4: candidate context construction, not LLM choice manipulation ✅
- P7: generic char limit, no skill-specific strings ✅
- P8: phase instructions untouched ✅
- care boundary: pre-call structural ✅ — Reyn が care すべき領域

**Effect** (B7 finding evidence):
- H-b1 (skill_improver desc 218→<80 chars only) → 0/5 (0%) empty stop
- H-b (1342→285 chars total) → 0/10 (0%) empty stop
- → expected 100% rescue for the observed attractor pattern

**Relationship to Option F**: Complementary, not redundant.
- Option G: prevents the trigger from forming (pre-call structural)
- Option F: surfaces residual cases via audit event + failure UX (post-call observability)

**Effort**: 1 day (implementation + Tier 2 tests + LLMReplay fixture re-record)
**ROI**: Very high — 1-day investment, 100% rescue for observed pattern

---

### Option F — Detect + explicit failure UX (no auto-rescue) — ADOPTED 2026-05-04

Surface the empty-stop as an explicit, user-visible failure without any
retry, context modification, or model escalation.

**Mechanism**: After `call_llm_tools`, check `_is_empty_router_response()`:
`finish_reason=="stop"` AND `content` empty AND `tool_calls` empty.

On match:
1. Emit `router_empty_response_detected` audit event (P6 compliance) with
   `finish_reason`, `completion_tokens`, `prompt_tokens`, `caller_hint`,
   `model` — all P7-clean (no skill/tool names in payload).
2. Put a localized failure message in the outbox (kind="agent") and return.
   No retry. No context change. No model switch.

**User principle alignment**: "これは llm の問題であって、 reyn で過剰ケアすべきで
はない。 retry すべきでない" (2026-05-04). Reyn's responsibility is observation
and surfacing — not absorption.

**P7 analysis**: Trigger condition (`finish_reason=stop AND content empty`)
is a provider-level API property, not a skill concept. Failure message and
event payload contain no skill/tool names. P7-clean.

**G4 signal**: Option F preserves the full attractor signal — every event
is audit-visible and countable. Option B would have suppressed 50% of events
via retry. Option F makes all events visible.

**Frequency measurement (B7-G12)**: 50% of identical payloads produce
empty-stop (probabilistic, not deterministic). Option F means ~50% of
attractor sessions produce a visible failure. This is the correct behavior
— LLM glitches are the LLM's problem; the user chooses remediation.

**Effort**: 0.5 day (predicate + event emit + i18n dict + Tier 2 tests).
**Effect**: Zero rescue, full observation. Converts silent blank-reply UX
into explicit failure UX with audit trail.
**ROI**: High for the Reyn principle. Low for short-term attractor mitigation.

## Decision

### Short term (within 1 week, no proxy prerequisite)

**Adopt Option G (description truncation) + maintain Option F (empty-stop
detection + clean failure UX).**

Rationale: B7 N-shot evidence established description verbosity as the
trigger. Truncation eliminates the trigger at root cause level (Option G).
Option F remains as the safety net for any residual cases where truncation
does not fully resolve (e.g., context evolution introduces new verbosity
sources).

The two options are complementary, not redundant:
- Option G: prevents the trigger from forming (pre-call structural environment)
- Option F: surfaces residual cases via audit event + failure UX (post-call observability)

Both align with care boundary: Option G is pre-call structural environment
integrity; Option F is post-call observability.

**User principle confirmed (2026-05-04)**: "コンテキストに問題がないのに空文字だった場合のケース、
これは llm の問題であって、 reyn で過剰ケアすべきではない。 retry すべきでない"

The decision aligns with the Reyn care boundary principle
(`docs/en/concepts/care-boundary.md`): post-call observable failure UX is
Reyn's responsibility, but auto-rescue (retry, escalation) is not.

**Option B (retry) — REJECTED**: Even with a 50% rescue rate (B7-G12
measurement), retry violates the user principle that LLM glitches are the
LLM's problem. Reyn must not silently absorb failures by re-invoking the
model. The G4 trigger signal must not be suppressed.

**Option C (hybrid escalation) — REJECTED**: Subsumes Option B's retry
violation. Additionally blocked by the same proxy prerequisite as Option A.

**Option G — Adopted** (implementation wave: a6127a46, batch 8 verify):

Truncate skill descriptions to ≤80 characters in both trigger paths:
1. `list_skills` tool_response builder in `router_loop.py`
2. system prompt inline skill list in `router_system_prompt.py`

`describe_skill` returns the full description (summary/detail pattern).

**Option F — Adopted** (shipped 2026-05-04):

Detect the `finish_reason=stop, content empty, tool_calls empty` signature
in `RouterLoop.run()` and respond with:

1. **Audit event** `router_empty_response_detected` (P6 compliance) with
   payload: `finish_reason`, `completion_tokens`, `prompt_tokens`,
   `caller_hint="router"`, `model`. Payload is P7-clean (no skill/tool names).

2. **User-visible explicit failure message** in the outbox (kind="agent"):
   - English: "The model returned an empty response. Please try rephrasing
     your request or check your configuration."
   - Japanese (output_language=ja): "モデルが空の応答を返しました。
     別の表現で再入力するか、設定を確認してください。"
   - Unknown language: falls back to English.

3. **No retry** — `call_llm_tools` is invoked exactly once per turn.
   No context modification. No model switch.

User-side remediation: rephrase the request, change model configuration,
or abort. Reyn's responsibility ends at observation.

Implementation: `_is_empty_router_response()` + `_EMPTY_RESPONSE_MSG` dict
in `src/reyn/chat/router_loop.py`. Tier 2 tests in
`tests/test_router_empty_response.py` (16 tests).

### Mid term — unchanged (may be deprioritized)

**Option C (hybrid weak + strong-model escalation)**: Remains deferred until
proxy exposes a strong model and G4 spike data confirms the attractor rate.
Option C would be evaluated as a user-configurable opt-in, not a default.

**Mid term may be deprioritized if Option G eliminates the attractor in batch 8 retest.**
If Option G (description truncation) reduces the empty-stop rate to 0% in
batch 8, the G4 spike priority drops significantly. Strong-model evaluation
would shift from "primary fix path" to "future scalability option."

### Deferred — unchanged

**Option D (tool_choice="required")**: Defer to G4 spike session for
measurement.

**Option E (per-session auto-resume)**: Scope deferred indefinitely.

**Option A (flat strong-model substitution)**: Not adopted as default.

## Consequences

**Positive (Option F, adopted):**

- G12 attractor events are audit-visible via `router_empty_response_detected`,
  queryable from the event log. G4 trigger signal is fully preserved.
- User receives an explicit, actionable failure message instead of a blank
  reply or silent hang. The F6/F7 invariant (no empty upstream reply) is
  also preserved for the multi-agent path — the failure message propagates
  upstream as a non-empty agent reply.
- Zero extra LLM calls. No cost delta per attractor event.
- P7-clean: no skill/tool names in event payload or failure message.
- Simple: `_is_empty_router_response()` is a 5-line predicate; no state
  machine, no retry budget, no model resolution overhead.

**Negative (Option F):**

- Attractor events are NOT rescued. User must re-input or change model.
  ~50% of turns affected by the G12 attractor (B7-G12 measurement) result
  in a visible failure message. This is intentional.
- Does not reduce the attractor rate — the G4 spike remains the primary
  long-term fix path.

**Precluded:**

- Silent blank-reply UX: any empty-stop event is now visible to the user.
- Auto-retry (Option B): explicitly rejected by user principle.
- Auto-escalation (Option C): rejected for same reason; remains deferred.
- D-track integration for router attractor (Option E): deferred indefinitely.

## Empty-stop frequency measurement (ADR 0021 follow-up)

**Measurement date**: 2026-05-04  
**Instrument**: `llm_replay.py --n 10 --diff` on a freshly captured attractor request  
**Source finding**: `docs/deep-dives/journal/dogfood/2026-05-04-batch-7-post-infra-verify/findings/B7-G12-empty-stop-frequency.md`

### Setup

Existing trace dumps (`llm_trace_h4.jsonl`, `llm_trace_h2.jsonl`, `llm_trace_h1.jsonl`,
`llm_trace_b8s1.jsonl`) contained no attractors. The attractor request recorded in
B7-RETRO-H4 (`fd2aef81-...`) was no longer on disk. A fresh dogfood run using the
same attractor-inducing input ("direct_llm skill を使って、カレーのレシピを教えてもらって")
captured a new attractor on the second attempt:

- request_id: `883da2c8-adf6-4cff-b86a-a9a540f423ee`
- context: `list_skills("") → list_skills("general") → stop` (same sequence as RETRO-H4)
- `completion_tokens=0`, MUST rule present in system prompt

### Results (n=10 replay)

| Outcome | Count | Rate |
|---------|-------|------|
| Empty-stop (attractor 再発) | 5 | **50%** |
| Rescued (tool_call: describe_skill) | 5 | 50% |

All rescued runs called `describe_skill("direct_llm")` with 18 completion tokens.
All empty-stop runs returned `completion_tokens=0, content=null`.

### Conclusions for Option B

1. **Probabilistic, not deterministic.** The attractor fires on ~50% of identical
   payloads. A single retry rescues ~50% of attractor events in expectation.
2. **Option B is justified.** The open question — "would retry rescue anything?" —
   is resolved: yes, with p≈0.5 per attempt. The "deterministic" failure mode
   that would make retry useless occurs only ~50% of the time.
3. **Residual risk.** ~50% of attractor events survive one retry. These are
   surfaced via the mandatory `router_attractor_retry` event and continue to
   provide G4 spike evidence.
4. **Option B priority unchanged.** The short-term recommendation (Option B) is
   maintained. Two retries would achieve ~75% rescue; cost delta is < $0.002/session.

## References

- `docs/deep-dives/journal/dogfood/2026-05-04-batch-7-post-infra-verify/findings/B7-RETRO-H4-attractor-prompt-evidence.md`
  — observation evidence closing the root cause
- `docs/deep-dives/journal/dogfood/giveup-tracker.md` G12 — full attractor history,
  prior design rationale, and OS-state-machine withdrawal reasoning
- `docs/deep-dives/journal/dogfood/g4-trigger-evaluation-spike.md` — G4 spike status
  (blocked) and proxy setup instructions
- `scripts/detect_attractor.py` — `_is_empty_response` / Heuristic 1 logic
  that Option B and C reuse
- `src/reyn/chat/router_loop.py` — attractor impact site (line 281–287,
  empty `put_outbox` on stop-without-content)
- `src/reyn/llm/llm.py` line 540 — `call_llm_tools` `tool_choice` parameter
- `docs/en/concepts/principles.md` — P3, P4, P7 full rationale
- [ADR-0012](0012-auto-resume-default.md) — skill-resume machinery (context
  for Option E scope assessment)
- [ADR-0013](0013-exception-aware-crash-lifecycle.md) — exception-aware
  lifecycle (retry policy precedent)
- [ADR-0020](0020-skill-only-permissions.md) — precedent ADR format
- `docs/deep-dives/journal/dogfood/2026-05-04-batch-7-post-infra-verify/findings/B7-G12-empty-stop-frequency.md`
  — empty-stop frequency measurement (n=10, rate=50%, probabilistic verdict)
- `docs/deep-dives/journal/dogfood/2026-05-04-batch-7-post-infra-verify/findings/B7-RETRO-H1-fix-verify.md`
  — `--patch` replay verification pattern (different attractor, same
  observation methodology; hallucination rate 57% → 0% confirmed)
- `docs/deep-dives/journal/dogfood/2026-05-04-batch-7-post-infra-verify/findings/B7-S1-fresh-retest.md`
  — Option F e2e behavior post-implementation (chain advances, empty stop
  surfaces clean failure UX)
- `docs/en/concepts/care-boundary.md` — Reyn の care 範囲 (structural /
  behavioral / gray) framework、 Option F 採用の design philosophy 根拠
- `docs/deep-dives/journal/dogfood/2026-05-04-batch-7-post-infra-verify/findings/B7-G12-context-root-cause.md`
  — N-shot --patch experiment proving description verbosity as the decisive
  trigger (4 hypotheses tested, only H-b shows effect; H-a MUST rule removal
  shows zero effect)
- `docs/deep-dives/journal/dogfood/2026-05-04-batch-7-post-infra-verify/findings/B7-G12-cross-attractor-pattern.md`
  — two trigger paths (list_skills tool_response / system prompt inline)
  confirmed across 5 attractor instances; both paths must be truncated for
  full rescue
