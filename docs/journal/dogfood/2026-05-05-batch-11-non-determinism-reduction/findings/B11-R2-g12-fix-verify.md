# B11-R2: G12 attractor fix verification — Pattern D (describe_skill routing strip)

| Field | Value |
|---|---|
| Date | 2026-05-05 |
| main HEAD at fix | `B11-R2 commit` (after `4898ef9`) |
| Worktree | `agent-aac6052f4e223b437` |
| Fix file | `src/reyn/chat/router_loop.py` — `_describe_skill()` strips routing + category |
| Constant | `_DESCRIBE_SKILL_STRIP_FIELDS` in `src/reyn/chat/router_tools.py` |

---

## Pre-fix rate measurement

**Payload**: B10-S5b trace (`9c4373af` request_id).

The B10 trace had describe_skill("eval_builder") returning **1381 chars** (full routing dict).

```
N=10 replay on B10 trace (pre-fix):
Finish reasons:
  tool_calls: 5
  stop: 5  ← G12 attractor (completion_tokens=0, content=null)
Rate: 50% (5/10)
```

**Supporting evidence (from earlier batches)**:
- B10-S5b-integration.md: 1 attractor in 4 dogfood attempts = 25% per session
- B7-G12-empty-stop-frequency.md: 50% rate at N=10

---

## Post-fix rate measurement

**Method**: Hypothesis B patch on B10 trace — replace `messages[9].content` (describe_skill
response) with routing-stripped version (187 chars vs 1381 chars).

```
N=10 replay on B10 trace (post-fix patched, routing stripped):
Finish reasons:
  tool_calls: 10
  stop: 0
Rate: 0% (0/10)
```

**Post-fix synthetic trace** (12 stdlib skills, 187-char describe response):

```
N=10 direct API calls:
  G12 attractor (empty stop): 0/10 (0%)
  Text stop (has content, ct > 0): 2/10
  Tool calls (correct): 8/10
```

Note: The text stops are NOT G12 attractors — they are clarification replies where the
LLM asked for more input. The `completion_tokens` > 0 and `content` has text, consistent
with the MUST rule behavior "explain in text why not".

---

## Fix summary

### Code change

`src/reyn/chat/router_tools.py`: Added `_DESCRIBE_SKILL_STRIP_FIELDS = frozenset({"routing", "category"})` with full rationale comment.

`src/reyn/chat/router_loop.py`: Updated `_describe_skill()` to return dict comprehension
filtering out `_DESCRIBE_SKILL_STRIP_FIELDS`.

Before:
```python
def _describe_skill(self, name: str) -> dict:
    for skill in self.host.list_available_skills():
        if skill.get("name") == name:
            return skill
    return {"error": f"skill not found: {name}"}
```

After:
```python
def _describe_skill(self, name: str) -> dict:
    for skill in self.host.list_available_skills():
        if skill.get("name") == name:
            return {k: v for k, v in skill.items() if k not in _DESCRIBE_SKILL_STRIP_FIELDS}
    return {"error": f"skill not found: {name}"}
```

### Response size reduction

| Skill | Full response | Stripped response |
|-------|--------------|------------------|
| eval_builder (B10, with project skills) | 1381 chars | ~200 chars |
| eval_builder (stdlib only) | 1002 chars | 187 chars |
| skill_improver (typical) | ~1500 chars | ~250 chars |

---

## Tests

### Tier 2 (Tier 2: structural invariants)

File: `tests/test_router_skill_description_truncation.py`

New tests added (B11-R2):
- `test_describe_skill_strips_routing_and_category` — routing/category absent from result
- `test_describe_skill_strip_fields_constant` — constant contains 'routing' and 'category'

Updated test:
- `test_describe_skill_returns_full_description` — docstring updated to reflect Pattern D fix
  (description verbatim, but routing stripped)

All 11 tests pass (9 original + 2 new). Full suite: **1012 passed, 2 xfailed**.

---

## Retest plan for Step 2

If a future dogfood session surfaces G12 Pattern D again:

1. Run `REYN_LLM_TRACE_DUMP=<path> reyn chat` with the S5b scenario
   (`eval_builder で direct_llm を analyze して、 target_skill=direct_llm`)
2. Check `detect_attractor.py` for `stop_with_must_rule` detections
3. If detected, inspect the describe_skill tool_response — confirm routing field is absent
4. If routing is present in the trace: check that `_DESCRIBE_SKILL_STRIP_FIELDS` is applied
   at call time (not after serialization)

---

## Verdict

**G12 Pattern D: resolved** at this commit.

Pre-fix: 50% G12 attractor rate (5/10 via replay).
Post-fix: 0% G12 attractor rate (0/10 via replay with routing stripped).

The fix is structural (pre-call environment shaping), P7-clean, and consistent with the
care-boundary framework. The B7 Option F detection infrastructure remains in place as
observability for any future residual attractor variants.

---

## References

- `B11-R2-g12-diagnosis.md` — root cause + hypothesis testing
- `B7-G12-truncation-fix.md` — previous Option G fix (Pattern A + C)
- `B7-G12-cross-attractor-pattern.md` — P-b mechanism analysis
- `docs/en/decisions/0021-g12-attractor-structural-fix-design.md` — ADR-0021
