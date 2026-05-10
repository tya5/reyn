# FP-0011: Remove `skill_narrator` — Let the Router LLM Narrate Skill Results

**Status**: **LANDED 2026-05-10 (= commit `59c991a`, A+B+C+D+E + Component B
anti-optimism strengthening). Follow-up: N≥10 flash-strong-tier retest to
confirm the 1/6 hallucination drops to ~0.**
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)
**Spike validated**: 2026-05-10 — see `docs/deep-dives/journal/dogfood/2026-05-10-fp-0011-narrator-removal-spike.md`. Three stages of context analysis flipped the spike framing from "regression" to "net quality improvement":

1. **narrator only fires on success path** (= 2/18 spike runs invoked narrator; failure-path narration has always been the router LLM's job). FP-0011 affects only the success-path narration, not failure-path.
2. **narrator hallucinates skill names** (= shot 1 narrator said "image_captioning, 4 files"; tool_result said "string_length"; router-narration correctly said string_length). narrator is not a trustworthy ground-truth source.
3. **router LLM uses tool_result as ground truth + overrides narrator's output** in the user-visible reply. Removing narrator eliminates one (unreliable) parallel output without losing quality.

Events-audited 18 runs yield 17/18 truthful (= 94%); the single hallucination (= narr-3 SE shot 2) occurred when router (flash) ignored `tool_result.status="error"` — unrelated to narrator removal.

**Recommendation**: land Components A + B + C + D + E as proposed. Component B SP guidance should be **tightened beyond the draft** to add explicit anti-optimism rule for `status="error"` / `data.error` field (= prevents the observed flash strong-tier failure-mode). N≥10 retest on flash strong tier post-strengthen recommended to confirm hallucination rate drops to ~0.

Driver findings during spike yielded 7 infra bugs + 1 architectural follow-up (= `R-PURE-MODE-REDEFINE` residual in plan file).

---

## Summary

Every other tool in the router (`recall`, `list_skills`, `read_memory_body`, etc.) relies on
the router LLM to turn structured results into a natural-language reply. `invoke_skill` alone
bypasses this by firing `skill_narrator` before the router LLM's next turn — creating both an
architectural asymmetry and a double-output risk. Remove `skill_narrator` and let the router
LLM narrate skill results the same way it narrates every other tool result.

---

## Motivation

### Asymmetry in narration responsibility

```
list_skills / recall / read_memory_body:
  router LLM → tool_call → structured result → router LLM narrates → user sees reply

invoke_skill today:
  router LLM → invoke_skill → skill runs
    → skill_narrator fires → reply pushed to outbox  ← user sees ①
    → tool result {"status": "finished", "data": {...}} accumulated into messages
    → router LLM called again → may generate text → user sees ②
```

The router LLM is already capable of narrating arbitrary structured results — it does so for
every other tool. There is no architectural reason `invoke_skill` results need a separate
narration path.

### Double-output risk

`skill_narrator` pushes `reply_text` to `outbox` **before** the router LLM's post-tool turn.
`invoke_skill` is registered as `dispatch_kind="sync"`, so the router loop continues and the
router LLM is called again with the tool result in context. If the router LLM generates any
text (rather than empty-stop), the user receives two independent replies.

```
narrator reply:  "The code review completed. Found 3 issues in auth.py."
router reply:    "Done! The skill finished successfully."   ← duplicate
```

### Empty-stop was treated as a bug, but was actually the intent

The G12 empty-stop attractor (router LLM exits with `finish=stop` and no content after
`invoke_skill`) was recorded as a reliability bug and patched repeatedly. In retrospect,
empty-stop was the correct router behaviour when narrator was doing the narrating. Removing
narrator resolves this contradiction: the router LLM should generate text, and it will.

### Extra LLM call per skill completion

`skill_narrator` is a pure-LLM phase (`allowed_ops: []`) — one full LLM call per skill run.
With narrator removed, every skill completion saves one LLM round-trip.

### Known quality issue: B2-M4

Dogfood finding B2-M4 (MED severity): narrator produces generic "skill completed" text
instead of extracting domain-meaningful fields from `final_output`. This is the
self-undermining failure of a dedicated narration skill — the router LLM, which already
handles heterogeneous tool outputs, is more robust in practice.

---

## Proposed implementation

### Component A — Remove narrator call from `session.py` (SMALL)

Delete `_invoke_narrator()` and `NARRATOR_SKILL_NAME`. In both `_run_one_skill()` and
`_run_skill_awaitable()`, remove the narrator call blocks (the `narrated` / fallback branches
that push to outbox). The tool result already flows back to the router loop unchanged.

```python
# _run_one_skill — REMOVE this block (~lines 2694–2739)
# narrated = await self._invoke_narrator(...)
# if narrated: ...
# else: fallback raw-dump ...

# _run_skill_awaitable — REMOVE this block (~lines 3075–3114) similarly
```

`_run_skill_awaitable` continues to return `{"status": ..., "data": ...}` as before —
the router LLM sees this as the `invoke_skill` tool result.

### Component B — Add post-`invoke_skill` guidance to router system prompt (SMALL)

The router SP currently has no instruction for what to do after `invoke_skill` returns.
Add a status-aware narration rule:

```
- After invoke_skill returns: reply in 1–2 sentences summarising what the skill did.
  Extract the field(s) that matter to the user from the result — do not dump raw JSON.
  Status guidance:
    "finished"             → confirm completion; optionally hint at the next step.
    "loop_limit_exceeded"  → say the skill ran out of phase budget; suggest re-running.
    other                  → describe what didn't complete; suggest the most likely fix.
```

This mirrors the `narrate.md` phase instruction, placed where it actually takes effect.

### Component C — Delete `skill_narrator` stdlib skill (SMALL)

Remove `src/reyn/stdlib/skills/skill_narrator/` entirely. Update `profile.py` to remove
narrator from the always-available skill allowlist bypass. Remove from `_KNOWN_SKILL_NAMES`
in tests.

### Component D — Remove narrator-specific tests (SMALL)

- Delete `tests/test_replay_narrator.py` (Tier 3a — replay tests for narrator LLM behaviour)
- Delete `tests/test_narrator_drift.py` (Tier 2b — drift detection invariants for narrator)
- Update `tests/test_router_loop_chatsession.py`: remove assertion that narrator is excluded
  from `available_skills` (narrator no longer exists)
- Update `tests/test_multi_agent_p7.py`: remove `skill_narrator` from `_KNOWN_SKILL_NAMES`

### Component E — Add Tier 2 contract test for post-invoke_skill narration (SMALL)

A new Tier 2 invariant test verifying that after `invoke_skill` succeeds, the router
produces a non-empty text reply (not empty-stop). This replaces the coverage previously
provided by the narrator tests.

---

## What does NOT change

- The `invoke_skill` tool definition and its `dispatch_kind="sync"` registration — unchanged.
- The tool result shape `{"status": ..., "data": ...}` returned to the router — unchanged.
- The router loop structure — unchanged.
- P6 `skill_run_completed` event — unchanged. Narration output is no longer persisted to
  history as `role=agent/source=narrator`; the router LLM's text reply is the history entry.

---

## Dependencies

None. This is a standalone removal.

---

## Cost estimate

**Total: SMALL**

| Task | Cost | Notes |
|---|---|---|
| Component A: remove narrator calls from session.py | SMALL | ~60 lines deleted, no new logic |
| Component B: router SP guidance | SMALL | ~5 lines added |
| Component C: delete skill_narrator + profile.py update | SMALL | directory removal |
| Component D: remove narrator tests | SMALL | 2 files deleted, 2 files updated |
| Component E: new Tier 2 invariant test | SMALL | 1 new contract test |

Risk: router LLM narration quality regression on weak models. Mitigated by Component B
(explicit SP guidance) and Component E (contract test). G4 spike (`gemini-2.5-flash`) run
recommended before landing to confirm narration quality with a stronger model baseline.

---

## Related

- `src/reyn/chat/session.py` — `_invoke_narrator`, `_run_one_skill`, `_run_skill_awaitable`
- `src/reyn/stdlib/skills/skill_narrator/` — skill to be removed
- `src/reyn/chat/router_system_prompt.py` — Component B insertion point
- `src/reyn/chat/profile.py` — narrator always-available bypass to remove
- `docs/deep-dives/journal/dogfood/2026-05-04-batch-2-real/findings/B2-M4-narrator-generic-completion.md`
