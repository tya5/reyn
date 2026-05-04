# Batch 5 Fix-Verify: Findings Index

**Date**: 2026-05-04
**HEAD at run**: `30fdc33` (B4-H1 + B4-H2 + prompt-consolidation applied)
**Scenarios**: A (curry/specialist) + B (skill_improver chain)

## Summary

| Scenario | Expected | Verdict | Notes |
|----------|----------|---------|-------|
| A: curry via specialist | B4-H1 fix confirmed | FAIL | B4-H1 never triggered — B5-H1 prompt regression で specialist never invokes |
| B: skill_improver chain | B4-H2 fix confirmed | PARTIAL | workspace created; eval cascade blocked by new B5-H2 |

## Findings

### New HIGH findings

| ID | Scenario | Description | Status |
|----|----------|-------------|--------|
| B5-H1 | A | prompt consolidation (`e90c0f2`) で 4 rule → 2 段落 化、 weak LLM が paragraph 内 MUST を低優先扱い → specialist が `list_skills` 後 invoke せず空 reply。 教訓 = 過剰 consolidation も regression | **fixed** at `ca116f3` (re-balance: 5 個別 bullet × 2 MUST 復活、 wording dedup のみ維持) |
| B5-H2 | B | `eval.run_target` が `run_skill` IR に full path で skill 参照 → `FileNotFoundError`。 Root cause は run target instruction wording (= skill name vs path 混同) | **fixed** at `fe91321` (`run_target.md` instruction で `skill` field 強制 + 誤 form 例示) |

### New MED findings

| ID | Scenario | Description | Status |
|----|----------|-------------|--------|
| B5-M1 | B | router が 1 review request に対し `skill_improver` を **3 並列** invoke (333k tokens / 51 LLM calls) | open → tracked as **G3** in [giveup-tracker](../../giveup-tracker.md) (= code-side dedupe 実装予定) |
| B5-M2 | B | `plan_improvements` + `apply_improvements` の最初の試行で invalid Control IR → `phase_retry` 発火 | open |

### Other observations

- **B4-H2 partial 動作**: `copy_to_work` の workspace 作成は成功 (= budget 3→6 + glob scope fix `d9787cb` の効果)、 ただし batch 5 では LLM が再び glob constraint 違反、 write skip パターンを観測 → 真の解 = preprocessor 化、 [giveup-tracker G2](../../giveup-tracker.md) で management

## Fix verification status

| Fix | Commit | Verified? | Notes |
|-----|--------|-----------|-------|
| B4-H1: narrator reply → agent_replies | `ffc9b4a` | NOT TESTED at b5 | prerequisite (specialist invoke) が B5-H1 で blocked、 batch 5 retest 2 (B5-H1 fix 後) で再確認予定 |
| B4-H2: copy_to_work budget + glob scope | `d9787cb` | CONFIRMED partial | workspace 作成成功、 ただし安定性に欠ける → preprocessor 化 (G2) で本質解消 |
| prompt consolidation | `e90c0f2` | **REGRESSION** | consolidation で signal 弱化 → B5-H1 → re-balance `ca116f3` で final |

## Lessons learned (memory に反映済)

[`feedback_prompt_design.md`](../../../../../.claude/projects/-Users-yasudatetsuya-Workspace-junk-claude-sandbox-sandbox-2/memory/feedback_prompt_design.md) で formalize:

- prompt rule の bloat / 重複 / 過剰適合は避ける ✅
- ただし **過剰 consolidation も regression を生む** (= signal 弱化)
- weak LLM への optimal balance: **個別 bullet × 1 MUST × wording dedup**

## Recommended next actions

1. ✅ **B5-H1 fix landed** (`ca116f3`) — re-balance prompt
2. ✅ **B5-H2 fix landed** (`fe91321`) — run_target instruction
3. 🟡 **batch 5 retest 2** (= B5-H1 + H2 fix 後の動作確認) — sonnet a79e で進行中
4. 🟡 **B5-M1 dedupe** — code-side rate limiter、 [giveup G3](../../giveup-tracker.md)
5. ⏳ **B4-M1** (eval.md path mismatch) と **B5-M2** は MED wave で取りまとめ
