# B7-RETRO-H2: Fix Design — list_skills input_artifact + input_fields hint

**Date**: 2026-05-04  
**Issue**: B7-RETRO-H2-eval-builder-hallucinate.md  
**Fix wave**: RETRO-H2 (Wave 2 of post-batch-7 infra fixes)

---

## Root cause recap

`invoke_skill` receives `{name, input}` where `input` is a generic dict.  
When the LLM skips `describe_skill`, it has no structural source for the correct
field names → hallucination (e.g. `agent_name` instead of `target_skill` for
`eval_builder`).

---

## Design candidates

### 案 A: list_skills result includes input_artifact + input_fields

`list_skills` response enriched with two fields per skill:
- `input_artifact`: artifact type name(s) from entry phase `input:` field  
  (e.g. `"user_message | eval_builder_request"`)
- `input_fields`: flat list of top-level property names from the structured  
  input artifact (e.g. `["target_skill"]`)

**pros**: 1 round-trip saved, LLM sees field names immediately after list_skills,  
P4 alignment (OS provides candidate structure)  
**cons**: list_skills response size increases; nested schema bloat avoided by  
including only field names (no types)  
**mitigation**: top-level fields only, type info omitted; absent when unavailable

### 案 B: invoke_skill description wording strengthened

Existing `(call describe_skill first if unsure)` strengthened to explicitly  
reference `list_skills' input_fields hint` as the first source.

**pros**: minimal, local change  
**cons**: prompt rules in gray zone (CLAUDE.md feedback_prompt_design); B7-S5a  
showed weak LLM ignores description-only guidance  
**alone**: insufficient — confirmed by empirical evidence

### 案 C: invoke_skill conditional schema (oneOf per skill)

JSON schema `oneOf` with per-skill input schema. Provider-native enforcement.

**pros**: schema-level rejection of wrong field names  
**cons**: payload grows linearly with skill count; `oneOf` unverified for Gemini;  
implementation complexity high  
**decision**: deferred to separate wave after provider compatibility verified

### 案 D (ADOPTED): 案 A primary + 案 B lightweight

- `list_skills` exposes `input_artifact` + `input_fields` (structural, pre-call)
- `invoke_skill` description adds 1-line hint referencing `list_skills' input_fields`

---

## care boundary alignment

| Fix | Category | Verdict |
|-----|----------|---------|
| 案 A: list_skills input hint | pre-call structural ✅ | Adopted |
| 案 B: description wording | gray zone (minimal) | Adopted as lightweight complement |
| 案 C: oneOf schema | pre-call structural ✅ | Deferred (provider compat) |
| OS state machine: describe before invoke | behavioral rescue ❌ | Rejected (P3 violation) |
| post-call retry on wrong field | post-call rescue ❌ | Rejected |

**Selected**: 案 D = 案 A + 案 B lightweight.  
案 B wording is kept minimal (1 sentence, no MUST rules) to stay below the  
prompt-bloat threshold confirmed in feedback_prompt_design.md.

---

## Implementation

### Files changed

| File | Change |
|------|--------|
| `src/reyn/chat/session.py` | `_extract_skill_input_hint()` helper + `enumerate_available_skills()` extended |
| `src/reyn/chat/router_loop.py` | `_skill_item()` static method; `_list_skills()` delegates to it |
| `src/reyn/chat/router_tools.py` | `invoke_skill` description updated (1-sentence hint) |
| `tests/fixtures/llm/router/chitchat.jsonl` | fixture keys updated (tool description changed hash) |
| `tests/fixtures/llm/router/invoke_skill_single_round.jsonl` | same |

### New test file

`tests/test_router_list_skills_input_hint.py` — 8 Tier 2 tests:

| Test | Coverage |
|------|----------|
| `test_list_skills_result_includes_input_artifact` | (a) input_artifact passed through |
| `test_list_skills_result_includes_input_fields` | (b) input_fields passed through |
| `test_list_skills_union_input_artifact_separator` | (c) union `\|` separator preserved |
| `test_invoke_skill_description_references_input_discovery` | (d) description mentions sources |
| `test_list_skills_no_input_hint_is_safe` | (e) safe fallback for skills without hint |
| `test_skill_item_is_generic_not_skill_specific` | (f) P7-clean (no skill-name hardcodes) |
| `test_extract_skill_input_hint_reads_stdlib_eval_builder` | (g) real-FS eval_builder smoke test |
| `test_extract_skill_input_hint_missing_phase_returns_empty` | (h) missing phase → empty dict |

### P7 compliance

`_extract_skill_input_hint` reads DSL files dynamically — no skill names hardcoded  
in OS code. `_skill_item` passes through whatever fields the catalogue supplies,  
not checking for any specific key name beyond `name`/`description`/`input_artifact`/  
`input_fields` (these are generic catalogue field names, not skill-domain strings).

---

## Verification results

- 946 passed, 2 xfailed (938 baseline + 8 new tests)
- 0 regressions
- All replay tests pass (fixture keys updated after tool description change)

---

## Residual concerns

1. **Weak LLM still skips list_skills entirely** — if the LLM calls `invoke_skill`  
   directly without `list_skills`, it won't see the hint. This is care boundary  
   gray zone (accepted). The enum constraint on `invoke_skill.name` (RETRO-H1)  
   still applies; only the input structure is unguarded.

2. **Provider compatibility for 案 C** — `oneOf` behavior on Gemini models needs  
   testing before conditional schema can be adopted. Tracked as separate wave.

3. **fix verify** — requires a new batch-8 dogfood run with `eval_builder`  
   invocation via `list_skills` (natural language entry point, no describe_skill).  
   Key metric: `target_skill` appears in LLM's invoke_skill.input.data, not  
   hallucinated alternatives.
