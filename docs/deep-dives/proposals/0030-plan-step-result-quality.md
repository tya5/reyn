# FP-0030: Plan Step Result Quality — Richer Output Guidance

**Status**: proposed
**Proposed**: 2026-05-14
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

The plan-step system prompt currently instructs steps to "Summarise what this step found in 1–3 sentences." This hard ceiling forces steps to discard code snippets, specific line numbers, function names, and key data — the exact details the synthesis step needs to produce a high-quality answer. Changing the guidance to allow richer output (code snippets, specific facts, structured data) with a soft character cap removes the artificial ceiling and lets the router synthesise from real evidence rather than summaries of summaries.

---

## Motivation

### Current guidance (after FP-0025)

```
Summarise what this step found in 1–3 sentences. Be factual; a separate
synthesis step will produce the user reply.
```

### What gets discarded

For a task like "what does the JWT decode logic in auth.py do?":

| With current guidance | With proposed guidance |
|---|---|
| "auth.py contains JWT decode logic at lines 78–95." | The actual function signature, key lines, and behaviour |
| "session.py manages session expiry." | The specific field names, TTL values, and edge cases |

The synthesis router receives summaries with no specifics. It cannot reconstruct the code snippet. The final answer is necessarily vague.

### Why a soft cap, not hard?

A hard "1–3 sentences" rule causes truncation regardless of content type. A soft 800-character cap:
- Allows a 10-line code snippet to be included verbatim
- Discourages wall-of-text dumps that bloat the synthesis context
- Is enforced by guidance, not syntax — the LLM can exceed it when warranted

---

## Proposed implementation

### 1. Update `build_plan_step_system_prompt` guidance (planner.py)

Current (after FP-0025):

```python
"Summarise what this step found in 1–3 sentences. "
"Be factual; a separate synthesis step will produce the user reply."
```

Proposed:

```python
"Report what this step found. "
"Include relevant code snippets, key facts, function names, line numbers, "
"or specific data directly — the synthesis step needs concrete evidence, "
"not paraphrases. "
"Keep your response under 800 characters where possible; "
"exceed the limit only when a code snippet or structured data requires it."
```

### 2. Stale description in `src/reyn/tools/plan.py`

`_PLAN_DESCRIPTION` still says:

```python
"The terminal step's text reply becomes the user-facing answer; "
"design the last step to synthesise"
```

This was true before FP-0025 C but is now stale — the router LLM synthesises from `step_results`, not from the terminal step. Update to:

```python
"After all steps complete, the router synthesises step results into "
"a final reply. Design each step to gather specific evidence "
"(code, facts, data); a dedicated synthesis turn handles the final reply."
```

---

## Target files

| File | Change |
|---|---|
| `src/reyn/chat/planner.py` | `build_plan_step_system_prompt` guidance text |
| `src/reyn/tools/plan.py` | `_PLAN_DESCRIPTION` stale text (post-FP-0025) |

---

## Dependencies

None. Guidance change only.

---

## Cost estimate

SMALL — text changes in two files. No logic changes.

---

## Verification

1. Run a plan step that reads a Python file → `step_results` contains actual code snippet / line numbers rather than a 1-sentence summary.
2. `_PLAN_DESCRIPTION` in `plan.py` no longer refers to the terminal step as the synthesiser.
3. Synthesis step produces a more specific answer with code evidence.

---

## Related

- `src/reyn/chat/planner.py` — `build_plan_step_system_prompt`
- `src/reyn/tools/plan.py` — `_PLAN_DESCRIPTION`
- FP-0025 (`0025-planner-narration-and-sp-fixes.md`) — introduced the synthesis separation this FP improves on
- FP-0027 (`0027-plan-step-failure-transparency.md`) — richer step results make failure gaps more visible
