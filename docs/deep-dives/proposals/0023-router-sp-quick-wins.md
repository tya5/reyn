# FP-0023: Router System Prompt — Quick Wins

**Status**: proposed
**Proposed**: 2026-05-13
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

Five targeted edits to `router_system_prompt.py` that improve LLM compliance,
cache efficiency, and JA UX without structural changes. All changes are additive
or reordering within the existing prompt; no new concepts are introduced.

---

## Motivation

Analysis of `router_system_prompt.py` (808 lines) against dogfood measurements
and industry best practices surfaced five independent issues, each fixable in
under a day.

### Issue 1 — Cache efficiency: static sections follow dynamic sections

Current section order:

```
Identity (static)
→ project_context (dynamic)
→ Role (static)
→ Intent axis (static)
→ skills / agents / memory / files / MCP (dynamic)
→ When asked what you can do (static)
→ Behaviour (static, ~5,800 chars — largest section)
```

Anthropic prompt cache requires a common prefix to hit. Because the first
dynamic section (`project_context`) appears early, everything after it —
including the large static `Behaviour` block — is excluded from the cache
prefix. Effective cache coverage: **~20%** (Identity only).

Reordering to place all static sections first raises coverage to **~60%**
(Identity + Role + Intent axis + When asked + Behaviour).

### Issue 2 — Intent axis explained twice

`## What you can do (intent axis)` (lines 161–221) describes the internal
routing labels (Action / Memory access / Save / Forget / Reply).

`## When asked what you can do` (lines 310–328) repeats the same categories
in user-facing language.

The LLM sees both. Risk: internal labels leak into user replies ("Your intent
is categorized as *Memory access*…"). Solution: merge into one section with
a clear internal-vs-user-facing split, or make internal labels non-lexical.

### Issue 3 — Multiple `MUST`s with no priority order in spawn-ack rules

Lines 521–564 list three MUST-level constraints for the spawn-ack response:
1. Reply with `/tasks` link
2. Keep reply to 1 sentence
3. Do NOT call invoke_skill again

When LLMs see multiple MUSTs at the same level, compliance degrades on all of
them. Dogfood batch history shows `/tasks` compliance was the most fragile.
Explicit prioritization (numbered, highest first) concentrates attention.

### Issue 4 — `delegate_to_agent` has no usage guidance in Behaviour

`delegate_to_agent` appears in the tool list (line 176) but has no Behaviour
rule explaining when or how to call it. The LLM must infer usage from the
tool description alone — a gap noted in every major vendor's prompt-writing
guide ("write usage guidance in the system prompt, not just the tool schema").

### Issue 5 — JA recall/memory disambiguation lacks JA examples

Lines 354–366 correctly distinguish `recall` (indexed search) from
`list_memory` / `read_memory_body` (memory ops) for EN inputs. But there
are no JA examples. Dogfood measurement showed JA examples reduced
non-compliance from ~50% to ~5% for routing rules (B12-R2). The same
technique should be applied here.

Missing coverage:
- `思い出して` / `前回の話` → should route to `recall` if indexed sources exist
- `覚えて` / `メモして` / `記録して` → should route to `remember_*`

---

## Proposed implementation

### Change 1 — Reorder sections for cache efficiency

Move all static sections to the top of `build_system_prompt()`:

```
[STATIC — cache prefix target]
1. Identity
2. Role statement
3. Intent axis (internal routing guide)
4. When asked what you can do
5. Behaviour

[DYNAMIC — varies per session]
6. project_context
7. Skills catalog
8. Agents catalog
9. Memory section
10. Indexed sources
11. Files section
12. MCP servers section
13. User capabilities list (conditional)
```

No content changes — pure reordering. Cache prefix covers all static sections.

### Change 2 — Merge intent axis duplication

Collapse `## What you can do (intent axis)` and `## When asked what you can do`
into a single section:

```markdown
## Capabilities (routing guide)

Internal routing axes — do NOT use these labels in user replies:
- Action: user wants something done → use invoke_skill / tools
- Memory access: user wants stored info → use list_memory / recall
- Save: user wants to store something → use remember_*
- Forget: user wants to delete stored info → use forget_memory
- Reply: conversational, no tool needed

When a user asks what you can do, answer in plain terms:
"I can run skills (…), search your documents (…), remember things (…)."
Do NOT say "Your intent is Action" or use any routing label in replies.
```

### Change 3 — Prioritize spawn-ack MUSTs

Replace the flat list with an ordered priority block:

```markdown
When invoke_skill returns {status: "spawned", ...}:

  Priority 1 (non-negotiable): Reply with the `/tasks` link so the user can
    track progress. This is the user's only visibility into the running skill.
  Priority 2: Keep your reply to 1–2 sentences. Do not elaborate or fabricate.
  Priority 3: Do NOT call invoke_skill again for the same request.
  Priority 4: Do NOT ask follow-up questions while the skill is running.
```

### Change 4 — Add `delegate_to_agent` Behaviour rule

Add to the Behaviour section after the invoke_skill rules:

```markdown
## Agent delegation

When a user task requires a peer agent (not a skill):
  call delegate_to_agent(to=<agent_name>, request=<user_query>)

Use this when:
  - The task is outside available skills but matches a peer agent's role
  - The user explicitly addresses a named agent

Do NOT delegate tasks that can be solved with available skills.
The peer agent responds asynchronously; acknowledge the delegation in 1 sentence.
```

### Change 5 — Add JA recall/memory examples

Extend the existing disambiguation block (after lines 354–366):

```markdown
Japanese input disambiguation:
  - 「思い出して」「前回の話」「あのとき言ってた〜」
      → recall (indexed search) — if indexed sources exist
      → list_memory / read_memory_body — if no indexed sources
  - 「覚えて」「メモして」「記録して」「保存して」「忘れないで」
      → remember_shared or remember_agent (memory write)
  - 「忘れて」「削除して」「消して」(about a memory entry)
      → forget_memory
```

---

## Target files

| File | Change |
|---|---|
| `src/reyn/chat/router_system_prompt.py` | All 5 changes above |

---

## Dependencies

None. All changes are within the single prompt-building function.
No schema, runtime, or skill changes required.

---

## Cost estimate

| Task | Cost |
|---|---|
| Reorder sections (Change 1) | SMALL |
| Merge intent axis (Change 2) | SMALL |
| Prioritize spawn-ack (Change 3) | SMALL |
| Add delegate_to_agent rule (Change 4) | SMALL |
| Add JA examples (Change 5) | SMALL |
| **Total** | **SMALL** |

Each change is an edit to a string-building function. No new abstractions,
no protocol changes. Changes 1–5 are independent and can be landed as one
commit or five.

---

## Verification

1. **Cache**: After Change 1, the static prefix (Identity through Behaviour)
   should be identical across turns with the same project. Verify with
   `--mode replay` that `cache_creation_input_tokens` drops after turn 1.
2. **Intent label leak**: After Change 2, run 10-shot dogfood — confirm zero
   occurrences of routing labels ("Action", "Memory access", etc.) in user
   replies.
3. **spawn-ack**: After Change 3, run N=10 invoke_skill scenarios — confirm
   `/tasks` appears in 100% of spawn-ack replies.
4. **Delegation**: After Change 4, run a task that matches a peer agent —
   confirm `delegate_to_agent` is called (vs. "I can't do that").
5. **JA recall**: After Change 5, test JA inputs — confirm `思い出して` routes
   to `recall` (not `list_memory`) when indexed sources are present.

---

## Related

- `src/reyn/chat/router_system_prompt.py` — sole target file
- FP-0024 (`0024-router-sp-semantic-tool-selection.md`) — medium-term follow-up
- Dogfood batches B12-R2, B13-R3 — JA examples measurement data
- Anthropic "Writing Tools for Agents" (2025) — tool description best practices
