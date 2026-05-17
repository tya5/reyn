# B36 Worker 2 Findings — stdlib_skills_core.yaml

**Batch**: B36  
**Worker**: W2 (stdlib_skills_core.yaml, 9 scenarios)  
**HEAD**: 0c1669f  
**Port**: 8082 (b36-2 worktree)  
**Date**: 2026-05-17  

---

## Aggregate Score

**V/I/R/B = 3/2/4/0** (9 scenarios)

| Scenario | Outcome | Key observation |
|---|---|---|
| index_docs_basic | REFUTED | Plan+web_search triggered; no rag tool in router |
| read_local_files_explain_source | INCONCLUSIVE | Correct reply (535 chars) but via file__read, not read_local_files skill |
| read_local_files_multi_file | REFUTED | Router loop exceeded max_iterations (5); list_actions loop |
| skill_builder_web_summariser | VERIFIED | skill__skill_builder alias invoked directly with correct args |
| word_stats_demo_sentence | VERIFIED | invoke_action→word_stats_demo; correct stats in reply (44 chars) |
| word_stats_demo_multiline | REFUTED | skill__word_stats_demo called with empty args {}; empty stats (0/0/0) |
| eval_run_direct_llm | REFUTED | Wrong skill: word_stats_demo spawned instead of eval |
| chat_compactor_long_session | VERIFIED | 5-turn, all turns substantive (2497-3981 chars); coherent final reply |
| chained_find_then_index | INCONCLUSIVE | Turn1 OK (1271 chars); Turn2 failed (no indexing tool found) |

---

## Alias Schema Verify (primary)

**alias_schema_visible: YES (partially)**

D2-min + D2-full alias schema embedding is **confirmed working** for:
- `skill__skill_builder`: RICH schema with `{skill_name, description, goal}` properties
- `skill__skill_improver`: RICH schema with 9 properties
- `file__*` aliases: RICH (all with correct properties)
- `web__*` aliases: RICH  
- `memory.operation__*`: RICH
- `reyn.source__*`: RICH

**Exception — `skill__word_stats_demo`: STUB** (empty properties, additionalProperties: True)

Root cause: `word_stats_demo` skill has no `input_schema` defined in its `skill.md` frontmatter (`input_schema: {}`). The D2-full `_resource_alias_metadata` code correctly reflects this as empty properties. This is correct behavior per the implementation — not a regression.

Sample alias schema (skill__skill_builder, request_id=1fc1e1df...):
```
description: "Generate a new skill from a natural-language description. Hot-list direct alias for skill 'skill_builder' — pass the skill's input fields as args; the dispatcher wraps them into the input artifact for invoke_action."
parameters.properties:
  skill_name: {type: string, description: "snake_case name..."}
  description: {type: string, description: "One sentence describing..."}
  goal: {type: string, description: "Detailed description..."}
required: [skill_name, description, goal]
```

The LLM **correctly invoked** `skill__skill_builder` with `{description: "...", skill_name: "web_article_summariser", goal: "..."}` — matching the declared schema exactly.

---

## Arg-Name Mismatch Recurrence (primary)

**arg_mismatch_recurred: NO (for skill__skill_builder); STRUCTURAL for skill__word_stats_demo**

Detailed findings:

1. **s4 (skill_builder_web_summariser)**: The LLM called `skill__skill_builder` with `{description, skill_name, goal}` — exact match to the schema. **No mismatch.** D2-full alias schema prevents the issue.

2. **s5 (word_stats_demo_sentence)**: LLM used `invoke_action` with `args={text: "The quick brown fox..."}`. The skill ran and produced correct output (44 chars). The arg `text` is not a formal schema field (skill has no input_schema) but the system still executed the skill from `user_message` context. **Functional success despite non-matching arg name.**

3. **s6 (word_stats_demo_multiline)**: `skill__word_stats_demo` called twice with empty args `{}`. This is because the alias schema correctly shows no properties (skill has no input_schema). The LLM had no field names to pass. Both runs got empty input (stats: 0/0/0). **Root cause: word_stats_demo has no input_schema — the alias correctly shows empty properties, but the skill requires user_message context passed implicitly, not via skill input fields.**

4. **s7 (eval_run_direct_llm)**: `invoke_action` with `{action_name: "skill__word_stats_demo"}` — missing `args`. But bigger issue: wrong skill selected entirely (see Hot-List Gap below).

---

## skill__index_docs Retest (B35 W2 S1)

**Scenario**: index_docs_basic  
**Outcome**: REFUTED  

The LLM went to **plan mode** and then `web_search` instead of invoking `skill__index_docs`. Event chain: `plan_emitted → plan_step_started → reyn_src_list → web_search`. No `skill_run_spawned` for index_docs.

The skill `index_docs` exists in stdlib but was **not present in the hot-list** (only `skill__skill_builder`, `skill__skill_improver`, `skill__word_stats_demo` appeared as skill aliases). The `rag.operation__create_index` tool (which index_docs uses) was also reported unavailable by the router reply: *"I cannot register the Markdown files in docs/concepts/ as a semantic index because the rag.operation__create_index tool is not available in the provided tool list."*

This is a **hot-list seed gap**: `index_docs` is not seeded in the hot list for this scenario.

---

## C1 / Q2 Stability

**C1 (skill routing)**: Unstable. s7 demonstrates C1 failure — LLM chose `skill__word_stats_demo` for an eval request because `skill__eval` and `skill__direct_llm` were not in the hot list. Hot-list only surfaced `[skill__word_stats_demo, skill__skill_builder, skill__skill_improver]` across all requests.

**Q2 (multi-turn coherence)**: Stable. s8 (5-turn chat_compactor) maintained context across all 5 turns with substantive, growing replies (2497→2679→3981→2827→3297 chars). No coherence degradation. s9 (2-turn chain) Turn 1 succeeded (1271 chars file listing) but Turn 2 failed on the index action.

---

## Key Findings

### F1: D2-full alias schema confirmed working for skill__skill_builder
The direct invocation of `skill__skill_builder` with correct args (`description`, `skill_name`, `goal`) in s4 provides positive evidence that D2-full alias schema embedding is reaching the LLM. The skill was created successfully and lint passed. **This is the first verified end-to-end success for a skill__ alias with full schema visibility in this worker.**

### F2: Hot-list skill coverage gap causes wrong-skill routing (s7, s1)
The hot-list for these sessions only contained `skill__word_stats_demo`, `skill__skill_builder`, `skill__skill_improver` as skill aliases. `skill__eval`, `skill__direct_llm`, `skill__index_docs`, `skill__read_local_files` were absent. This caused:
- s7: LLM chose `word_stats_demo` for an eval request (closest available skill)
- s1: LLM went to plan/web_search rather than index_docs

The hot-list is usage-history-seeded. These skills had no prior usage in this fresh workspace, so they never appeared. **This is expected behavior but surfaces a discoverability gap for first-use scenarios.**

### F3: word_stats_demo skill has no input_schema (expected, but surfaces alias stub)
`skill__word_stats_demo` alias shows empty properties and `additionalProperties: True`. This is correct per the implementation (skill.md has no input_schema). The skill takes input via `user_message` context not via skill input fields. The alias description does not explain this, so LLMs calling the alias directly (s6) pass no args and get empty stat results.

### F4: Router loop (s3 = read_local_files_multi_file)
s3 hit `Router loop exceeded max iterations (5)`. The LLM made 3 `list_actions` calls followed by 2 file tool calls but couldn't converge. This is a structural attractor: without `file__read` or `file__glob` in the initial hot list, the LLM used `list_actions` to discover them, consuming iteration budget before completing the task.

### F5: read_local_files_explain_source routed via file__read (not skill)
s2 got a correct, substantive reply (535 chars) about `cron/scheduler.py` via `invoke_action → file__read`. The `read_local_files` skill was not in the hot list. The LLM correctly adapted to use the available file tool directly. **Rubric-wise partially satisfying but not the intended skill path.**

---

## Observations on D2-min / D2-full Impact

Positive evidence (direct observations):
- `skill__skill_builder` RICH schema → LLM invoked with correct 3-field args → skill completed (VERIFIED)
- `file__glob`, `file__read`, `web__search`, `web__fetch` RICH schemas → LLM invoked these tools correctly in s3, s9

Not tested by this batch (skills absent from hot list):
- `skill__eval` alias schema (eval not hot-listed, never visible to LLM)
- `skill__index_docs` alias schema (index_docs not hot-listed)
- `skill__read_local_files` alias schema (not hot-listed)

**Hypothesis**: B35 W2 S1 `skill_run_failed` from unsafe-python gate may have been masked by the hot-list gap — the skill never got dispatched in B36 either, just for a different reason (not in hot list vs unsafe-python gate).

---

## ΔvsB35

B35 W2 aggregate was V=0, I=2, R=7, B=0 (from journal). B36 W2: V=3, I=2, R=4, B=0.
**ΔvsB35 = +3V** (s4 skill_builder, s5 word_stats_sentence, s8 chat_compactor all newly verified).

The +3V is consistent with the A2A driver improvement (reply capture working) confirmed in B35, plus D2-full alias schema enabling correct skill__skill_builder invocation.
