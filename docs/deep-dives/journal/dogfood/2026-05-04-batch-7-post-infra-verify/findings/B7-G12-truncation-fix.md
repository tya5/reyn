# B7-G12: Skill Description Truncation Fix

**Date**: 2026-05-04  
**main HEAD at fix**: b9f72a6  
**Finding origin**: a62a9dad (H-b1 verification) + a947255e (cross-attractor pattern)  
**Tier**: Tier 2 structural fix (pre-call environment, care boundary aligned)

---

## Setup

Observed G12 empty-stop attractor: router receives a domain-task request,
optionally calls `list_skills`, then emits `finish_reason=stop` with no content
and no tool_calls (~50% rate with gemini-2.5-flash-lite).

Root cause confirmed in:
- `B7-G12-context-root-cause.md` (a62a9dad): H-b1 verification — truncating
  `skill_improver` description from 218 chars → ≤80 chars reduced empty-stop
  rate from 10/10 (100%) → 0/10 (0%).
- `B7-G12-cross-attractor-pattern.md` (a947255e): two distinct trigger paths
  confirmed — Pattern A (via `list_skills` tool_response) and Pattern C (via
  system prompt inline skill list, router stops before calling `list_skills`).

---

## Implementation outline

### Constant

`MAX_DESC_LEN_FOR_LISTING = 80` added to `src/reyn/chat/router_tools.py`.
Docstring cites B7 finding and the H-b verification commit (a62a9dad).

### Pattern A fix — `list_skills` tool_response

`RouterLoop._skill_item()` in `src/reyn/chat/router_loop.py`:
- Imports `MAX_DESC_LEN_FOR_LISTING` from `router_tools`.
- Truncates `description` to `desc[:80] + "..."` when `len(desc) > 80`.
- Short descriptions (≤80 chars) returned verbatim — no spurious ellipsis.

### Pattern C fix — system prompt inline skill list

`build_system_prompt()` in `src/reyn/chat/router_system_prompt.py`:
- Imports `MAX_DESC_LEN_FOR_LISTING` from `router_tools`.
- Same truncation logic applied to each skill's description in the
  "Available skills" flat list section.

### `describe_skill` — full description preserved

`RouterLoop._describe_skill()` is a separate code path that returns the full
skill catalogue entry dict without passing through `_skill_item()`.  No change
needed; verified by Tier 2 test (e).

### Truncation format

`desc[:MAX_DESC_LEN_FOR_LISTING] + "..."` — simple char-limit truncation.
Sentence-boundary smart truncation deferred (simplicity-first; batch 8 retest
will determine if further refinement is needed).

---

## Tier 2 test list

File: `tests/test_router_skill_description_truncation.py`

| Test | Coverage |
|------|----------|
| `test_list_skills_long_description_truncated` | (a) Pattern A: truncation fires for long desc |
| `test_list_skills_truncation_appends_ellipsis` | (b) Format: first 80 chars + "..." |
| `test_list_skills_short_description_untouched` | (c) Short/exact-length desc untouched |
| `test_system_prompt_long_description_truncated` | (d) Pattern C: system prompt truncation |
| `test_system_prompt_short_description_untouched` | (d) Pattern C: short desc verbatim |
| `test_describe_skill_returns_full_description` | (e) describe_skill returns full desc |
| `test_list_skills_backward_compat_fields_preserved` | (f) name/input_artifact/input_fields preserved |
| `test_all_stdlib_skills_description_within_limit` | (g) all 12 stdlib skills pass |
| `test_max_desc_len_constant_is_80` | (h) constant value = 80 |

All 9 tests pass. Full suite: 979 passed, 2 xfailed (was 970 + 9 new).

---

## Care boundary alignment

This fix is **structural pre-call environment preparation** — the exact
primary care area defined in `docs/en/concepts/care-boundary.md`:

- ✅ We shape the LLM's input context (description length in tool_response and
  system prompt) before the LLM call.
- ✅ No post-call rescue: we do not inspect the LLM's output and retry or patch it.
- ✅ Not skill-specific: `MAX_DESC_LEN_FOR_LISTING` is a generic OS-level
  threshold applied to all skills identically (P7 clean).
- ✅ LLM retains full details on demand via `describe_skill` (details-on-demand
  pattern intact).

---

## Residual concerns

1. **80-char threshold validity**: H-b verified 0/10 empty-stop at ≤80 chars.
   Whether this holds at a wide variety of skill configurations and user inputs
   is untested.  Batch 8 dogfood retest will provide cross-config evidence.

2. **Sentence-boundary truncation**: Current implementation cuts at exactly 80
   chars, potentially mid-word or mid-clause.  The truncated description may
   be grammatically awkward.  For now, simplicity is prioritized; sentence-
   boundary smart truncation is deferred pending batch 8 observations.

3. **Cross-model generalization**: H-b used gemini-2.5-flash-lite exclusively.
   The 80-char threshold may need adjustment for other weak models if the
   attractor manifests differently.

4. **LLMReplay fixture**: System prompt content changes (descriptions now
   truncated) mean cached fixtures may drift if re-recorded.  No LLMReplay
   fixture re-record was performed for this wave; batch 8 will serve as the
   functional validation path.
