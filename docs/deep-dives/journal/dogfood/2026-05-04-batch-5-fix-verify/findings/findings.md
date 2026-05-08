# Batch 5 Fix-Verify — Findings

> 「fix した」 と「使えるようになった」 の最大の gap が surface した batch。
> B4-H1 + B4-H2 の fix が両方とも **prereq blocked** で機能検証できず、
> 代わりに新 HIGH 2 件 (B5-H1 / B5-H2) が露呈。 user feedback を額面通り
> 適用した consolidation refactor が逆 regression を生むという、 process と
> 信号設計の両面で学びの濃い回。

**Date**: 2026-05-04
**HEAD at run**: `30fdc33` (B4-H1 + B4-H2 + prompt-consolidation applied)
**Scenarios**: A (curry/specialist) + B (skill_improver chain)

## 概要

| ID | 重要度 | 一行で言うと | 状態 |
|---|---|---|---|
| [B5-H1](B5-H1-prompt-consolidation-regression.md) | HIGH | prompt consolidation (`e90c0f2`) で signal 弱化、 specialist 再び list_skills 後空 reply | **fixed** at `ca116f3` (re-balance) |
| [B5-H2](B5-H2-run-target-path-form.md) | HIGH | `eval.run_target` が path で run_skill 呼び FileNotFoundError → `KeyError: 'name'` | partial fix at `fe91321` (instruction)、 残り root cause は **G2** (`763c86c`) で解消見込み |
| B5-M1 | MED | router が 1 request に対し `skill_improver` 3 並列 invoke (333k tokens) | open → tracked as **G3** in [giveup-tracker](../../giveup-tracker.md) |
| B5-M2 | MED | `plan/apply_improvements` の最初の試行で invalid Control IR → `phase_retry` | open |

## ハイライト narrative

### user feedback の善意で signal 弱化を生んだ — B5-H1

batch 1-3 で `router_system_prompt.py` に MUST rule を 4 件積み重ねた
(F3+F9 / B2-H1 / B3-H1+M3)。 これに対し user feedback 「肥大化、 重複、
過剰適合に気をつけましょう」 が来た。 私はこの方向感を memory `feedback_
prompt_design.md` に formalize し、 続いて `e90c0f2` で 4 rule を 2 段落に
**consolidation refactor** した。 LOC -33%、 MUST count 3 → 1。

batch 5 fix-verify Scenario A で specialist が再び `list_skills` 後に空 reply
を返した (= **B3-H1 attractor の再発**)。 deep dive で、 weak LLM (gemini-
2.5-flash-lite) は **paragraph 内の複数 sentence 形式の MUST を 1 段落 = 1
priority signal** として扱うことが判明。 「個別 bullet × 各 1 MUST × N 件」 と
「段落 × N sentence × 1 MUST」 は signal 強度が非対称。

修正 `ca116f3` は consolidation を partial revert。 5 個別 bullet × 各 1 MUST
形式に戻し、 wording dedup のみ維持。 教訓は memory に追記:

> bloat も regression、 過剰 consolidation も regression。 weak LLM への
> optimal balance は **個別 bullet × 1 MUST × wording dedup**。

これは「user feedback の方向 vs 過剰適用の境界線」 を私が事前に判断できなかった
事例。 詳細は [B5-H1](B5-H1-prompt-consolidation-regression.md) と
[batch 5 retrospective](../retrospective.md) を参照。

### 同じ error message、 異なる root cause 2 つ — B5-H2

Scenario B で `control_ir_failed: {"kind": "run_skill", "error": "'name'"}` が
8 件出て eval cascade 全 score 0.0。 当初は run_skill IR op の handler が
`name` field を要求するのに LLM が `path` で渡している、 と解釈した。 instruction
を「`skill` field 強制」 で修正 (`fe91321`)。

batch 5 retest 2 で fe91321 fix 後を verify すると、 instruction fix は機能
✅ ( = run_target が `skill:` 出力)、 ところが **同じ `'name'` error が persist**。
deeper investigation で第 2 root cause 判明: **`copy_to_work` が source content
を read しているが write op で content を omit、 0-byte file を生成**。
後続 `parse_skill` が空 frontmatter で `KeyError: 'name'` を raise。

つまり「`'name'`」 という同じ error message を 2 つの root cause が共有。 第 1
fix で半分解消、 残り半分は G2 ([copy_to_work preprocessor 化](../../giveup-tracker.md)、
`763c86c`) で構造的解消見込み。 詳細は [B5-H2](B5-H2-run-target-path-form.md)。

## Fix verification status

| Fix | Commit | Verified? | Notes |
|-----|--------|-----------|-------|
| B4-H1: narrator reply → agent_replies | `ffc9b4a` | NOT TESTED at b5 | prerequisite (specialist invoke) が B5-H1 で blocked、 batch 5 retest 2 で ✅ confirmed |
| B4-H2: copy_to_work budget + glob scope | `d9787cb` | CONFIRMED partial | workspace 作成成功、 ただし安定性に欠ける → 真の解 G2 (`763c86c`) で resolved |
| prompt consolidation | `e90c0f2` | **REGRESSION** | consolidation で signal 弱化 → B5-H1 → re-balance `ca116f3` で final |

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
