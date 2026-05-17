# H4 description disambiguator ablation

Generated: 2026-05-17
HEAD: c8fae2e (feat/fp-0034-phase1-universal-catalog)
Worktree: /tmp/reyn-ablation/H4-description
Ablation agent: ablation-h4

---

## Hypothesis

H4: A measurable fraction of refuted scenarios stem from the **4-way skill
description ambiguity** (`skill__skill_builder` / `skill__skill_improver` /
`skill__skill_importer` / `skill__eval` all share description shape). Sharper
disambiguators in `skill.md` description fields should flip wrong-skill-invocation
scenarios.

---

## Pre-patch description audit (baseline)

| Skill | B32 description (seen by LLM via hot-list alias) |
|---|---|
| `skill__skill_builder` | `Generate a new skill from a natural-language description. Use this direct alias...` |
| `skill__skill_improver` | `Iterate a skill to improve it: repeatedly run eval, plan DSL changes, apply them... (B29 already patched)` |
| `skill__skill_importer` | `Search a public skills registry, let the user pick a candidate, and import. Use this direct alias...` |
| `skill__eval` | `Run a single eval case: score a target skill... (B29 already patched)` |

B29 (commit `14c6b6b`) already patched `eval` and `skill_improver` with decisive leading verbs
("Run" / "Iterate"). This ablation targets the remaining 2: `skill_builder` and `skill_importer`.

### Pre-patch keyword overlap (all 4 descriptions)

| Pair | Token overlap |
|---|---|
| skill_builder vs skill_improver | 6 shared tokens (a, does, improve, not, output, skill) |
| skill_builder vs skill_importer | 10 shared tokens (multiline description shares format tokens) |
| skill_builder vs eval | 5 shared tokens |
| skill_improver vs eval | 9 shared tokens (B29-fixed pair) |
| skill_importer vs eval | 7 shared tokens |
| skill_improver vs skill_importer | 5 shared tokens |

---

## Patch applied (worktree only)

### skill_builder/skill.md

```diff
-description: Generate a new skill from a natural-language description
+description: "Create a NEW skill from a natural-language spec. Output: working skill in reyn/local/. Does NOT improve an existing skill — use skill_improver for that."
```

Leading verb: **Create** | char count: 152 | negative pointer: "Does NOT improve — use skill_improver"

### skill_importer/skill.md

```diff
-description: |
-  Search a public skills registry, let the user pick a candidate, and import
-  the chosen skill as a multi-phase reyn skill under reyn/local/.
+description: "Import a skill from an external source / registry. Input: source URL or path. Output: installed skill under reyn/local/. Does NOT create or modify skill content."
```

Leading verb: **Import** | char count: 161 | converts multiline to single-line

### Post-patch 4-way leading verb matrix

| Skill | Leading verb |
|---|---|
| skill_builder | **Create** |
| skill_improver | **Iterate** |
| skill_importer | **Import** |
| eval | **Run** |

All 4 descriptions now lead with a unique decisive verb. Pairwise overlap max: 10
(skill_builder vs skill_importer — both mention reyn/local/; irreducible domain terms).

### Test suite (existing)

```
tests/test_skill_description_disambiguation.py — 5/5 PASSED
```

(The existing test suite covers only eval vs skill_improver per B29. No new tests added in
this ablation — scope is ablation-only, not production landing.)

---

## llm_replay ablation — per-scenario results

### Method

Fresh traces captured via `REYN_LLM_TRACE_DUMP` + `reyn chat ablation-h4 --cui`.
Each first-router-turn request replayed N=3 to N=5 times via `scripts/llm_replay.py`
with and without `--patch`.

### Scenario: W2 S7 — eval_run_direct_llm (PRIMARY)

**B32 verdict**: inconclusive (routing was correct per NEW-2 verification;
3x parallel evals all interrupted by stdin close)

**Ablation setup**: In this fresh clean-state environment, `skill__eval` is NOT
in the hot list (14 tools: universal wrappers + 4 skill aliases, excludes eval).
This reproduces the pre-NEW-2 condition: LLM must route without seeing eval's alias.

**Baseline (no patch), N=5**:
- 4/5 invoked `skill__skill_improver` (wrong skill, wrong action)
- 1/5 invoked `skill__skill_eval` (hallucinated non-existent name)
- 0/5 invoked `skill__eval` (correct)

**Patched (skill_builder + skill_importer changed), N=5**:
- ~4/5 invoked `skill__skill_improver` (still wrong)
- 1/5 no explicit action_name in args (malformed)
- 0/5 invoked `skill__eval` (correct)

**Verdict**: NO FLIP. The patch has no measurable effect on eval-vs-improver
misrouting. Root cause: the misrouting is eval↔improver, not builder↔importer.
The B29 fix already changed eval and improver descriptions; the remaining problem
is that `skill__eval` is not in the hot list, so the LLM never sees the
"Run a single eval case" description at all.

### Scenario: W6 s-fp11-1 — builder-invalid-spec (DOUBLE DISPATCH)

**B32 verdict**: refuted (double dispatch + session carryover contamination)

**First router turn — baseline (no patch), N=5**: 5/5 invoked `invoke_action(skill__skill_builder)` (correct)
**First router turn — patched, N=5**: 5/5 invoked `invoke_action(skill__skill_builder)` (correct)

**Verdict**: NO FLIP. Routing was correct in both conditions. Double dispatch
in B32 was caused by session carryover (history.jsonl not wiped) + multi-turn
re-prompting, NOT by description ambiguity at the first-turn routing layer.
The patch is not relevant to this failure mode.

### Scenario: W7 S1 — scenario_1_reyn_research_chain (AMBIGUITY COMPLAINT)

**B32 verdict**: refuted (true_band) — "Turns 2-5 all refused: same-description
tools ambiguity." N=1 observation.

**Ablation**: Fresh 5-turn reproduction attempted. Observed: agent completed all
5 turns without explicit ambiguity refusal (different outcome from B32 N=1 run).
LLM called `describe_action` on skill tools in turn 2 (ambiguity-seeking behavior)
then answered inline.

**Turn 2 baseline (no patch), N=3**: 3/3 called `describe_action` + 2/3 `invoke_action`
**Turn 2 patched, N=3**: 3/3 called `describe_action` (MORE, not fewer)

**Verdict**: NO FLIP observed. The ambiguity complaint in B32 W7 S1 was a N=1
probabilistic event that did not reproduce consistently. The description patch
does not prevent `describe_action` calls — it may slightly increase them by making
the skill descriptions more distinctive (curiosity effect). The W7 S1 "refusal"
pattern needs N≥5 reproduction to confirm before attributing to descriptions.

### Scenario: W2 S4 — skill_builder_web_summariser

**B32 verdict**: inconclusive (routing correct; interrupted by stdin close)

**Baseline, N=5**: 5/5 invoked `invoke_action(skill__skill_builder)` (correct)
**Patched, N=5**: 5/5 invoked `invoke_action(skill__skill_builder)` (correct)

**Verdict**: NO FLIP (no routing error to fix; scenario was interrupted, not misrouted)

### Scenario: W6 narr-3 — skill-builder triple dispatch

Not independently captured (same root cause as s-fp11-1: session carryover + multi-turn).
Attribution: history.jsonl contamination, not description-layer ambiguity.

---

## Per-scenario before/after summary

| Scenario | B32 verdict | Patched verdict (replay) | Routing before | Routing after |
|---|---|---|---|---|
| W2 S7 eval_run_direct_llm | inconclusive | no change | 0/5 eval correct | 0/5 eval correct |
| W6 s-fp11-1 builder-invalid-spec | refuted | no change | 5/5 correct | 5/5 correct |
| W7 S1 reyn_research_chain | refuted (N=1) | no change | describe_action 3/3 | describe_action 3/3 |
| W2 S4 skill_builder_web_summariser | inconclusive | no change | 5/5 correct | 5/5 correct |
| W6 narr-3 triple dispatch | inconclusive | not tested | — | — |

---

## Quantitative

- N targeted scenarios: 4 (with replay data)
- N flipped to verified/inconclusive: 0
- Flip rate: 0/4 = 0%
- Conclusion: **not-description-bound** (K/N < 0.2)

---

## Root cause analysis (per-scenario)

### W2 S7 (eval misroute): description is NOT the layer

The eval↔improver misrouting persists because `skill__eval` is absent from the
hot-list in clean sessions. When `skill__eval` is not directly visible as a named
alias, the LLM uses `invoke_action` with `action_name="skill__skill_improver"` —
reaching for the visible evaluation-flavored skill. The B29 fix to eval/improver
descriptions helps when both are visible; it does not help when eval is invisible.

**True fix layer**: ensure `skill__eval` appears in the LLM's hot list on cold
start. B30-NEW-2 is supposed to seed it. The wipe recipe (deleting action_usage.jsonl)
prevents accumulation, which may suppress `skill__eval` from the hot list depending
on how tracker state is initialized per run.

### W6 double-dispatch: description is NOT the layer

Triple/double dispatch is a multi-turn re-invocation pattern triggered by the user
re-phrasing or the agent asking clarifying questions. Each new user turn triggers
a new `invoke_action`. The root cause is a lack of per-turn invocation deduplication,
not description ambiguity. B32 §4.5 correctly notes this needs "separate issue +
per-turn invocation guard."

### W7 S1 ambiguity refusal: N=1, not reproducible

The B32 W7 S1 "same-description" complaint was a single observation. Fresh
reproduction (5 turns) did not reproduce the explicit refusal. The LLM called
`describe_action` on skill tools (ambiguity-seeking) but ultimately responded.
Per the pre-conclusion checklist: 1/N inspection — cannot conclude the description
patch flips this scenario.

---

## Conclusion

**H4 is not confirmed.** The 4-way description disambiguator patch (skill_builder
"Create" + skill_importer "Import" leading verbs) does not flip any of the 4 tested
refuted scenarios. Flip rate = 0/4 (0%).

The patch is still **structurally correct and worth landing** as a hygiene improvement:
- 4 unique leading verbs (Create / Iterate / Import / Run) prevent future confusion
- All pairwise overlaps ≤ 10 tokens
- Single-line format for skill_importer (was multiline, truncated in hot-list view)
- Existing test suite passes (5/5)

However, the B32 refuted scenarios that prompted this ablation have different root
causes:
1. **eval misrouting** → seed/hot-list layer: skill__eval not visible when cold
2. **double-dispatch** → envelope layer: per-turn invocation deduplication guard
3. **W7 S1 ambiguity complaint** → N=1 probabilistic, not structurally reproducible

The memory `feedback_envelope_layer_fix.md` prediction holds: description-layer fixes
address schema-layer misidentification, but the observed failures are at the
hot-list visibility layer and the per-turn dispatch envelope layer.

---

## Patch artifact

See `/tmp/reyn-ablation/H4-description/patch.diff`

Patch is worktree-local only. Not committed to main.

## Recommendation

Land the description patch as hygiene (small PR, no test changes needed beyond
verifying existing `test_skill_description_disambiguation.py` passes). It does not
fix B32 refuted scenarios but prevents future description-class confusion as the
catalog grows.

For B32 refuted scenarios, fix the correct layers:
- `skill__eval` hot-list: investigate why clean sessions miss it despite seed fix
- double-dispatch: per-turn invocation guard (B32 §4.5 issue)
