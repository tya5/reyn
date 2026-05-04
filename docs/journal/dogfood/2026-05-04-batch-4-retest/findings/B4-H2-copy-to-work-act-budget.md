# B4-H2 [HIGH]: copy_to_work、 act budget の中で写経すらできない

> 一行で: skill_improver chain で `copy_to_work` phase が `max_act_turns: 3` を
> glob+read だけで使い切り、 write step 到達せず → workspace 未作成 → eval
> cascade 全 score 0.0。 instruction 強化と budget 拡大で延命したが、 真の解は
> G2 = preprocessor 化。

| Field | Value |
|---|---|
| Severity | HIGH |
| Status | partial fix at `d9787cb` (3→6 + glob scope) → batch 5 で write skip 再現 → **真の解 fixed** at `763c86c` ([giveup G2](../../giveup-tracker.md) resolved) |
| Scenario | S3 (skill_improver nested chain) |
| Found | 2026-05-04 |
| Raw observation | [B4-S3-skill-improver-nested.md](B4-S3-skill-improver-nested.md) |

---

## 観測

S3 で skill_improver chain (= 最深 4 階層 nested run_skill chain を観測する
意図) を起動。 階層は **3 layer** で動いた:

```
L1: skill_improver (run_id=...)
  L2a: eval_builder      (prepare phase で起動)
  L2b: eval              (run_and_eval phase で起動)
    L3: direct_llm       (eval.run_target で起動 — エラーで終了)
```

ただし L2b の eval cascade で `[Errno 2] No such file or directory:
'.reyn/skill_improver_work/direct_llm/skill.md'` が連発。 4 eval run × 2
attempt = 8 件 `control_ir_failed`。 結果 eval が score 0.0、 skill_improver
chain は改善案を出さず終了。

WAL trace で `copy_to_work` phase の挙動を観測:

```
phase: copy_to_work (max_act_turns=3)
  act turn 1: file/glob src/reyn/stdlib/skills/**/*.md  → 39 files matched
  act turn 2: file/read direct_llm/skill.md            → ok
  act turn 3: file/read direct_llm/phases/answer.md     → ok
  → max_act_turns 到達、 force finish
  → file/write 一切呼ばれず、 .reyn/skill_improver_work/direct_llm/ 未作成
```

## つまり何が起きたか

`copy_to_work` phase は LLM-driven な「`<original_dsl_root>` を glob + read +
write」 を act loop で実行する設計だった。 LLM の attractor:

1. **glob を broad に取りすぎ**: instruction で `<original_dsl_root>/skill.md`
   を期待していたが、 LLM は親 dir で glob して 39 skill 全部 match
2. **read を redundant に反復**: 大量 file を read しようとして act turn を
   消費
3. **write 到達せず**: `max_act_turns: 3` を read で使い切り、 write step に
   行かない

instruction で「target slug のみ glob」 を強調しても LLM は honor せず。
これは **LLM の glob+read attractor が wording で抑え込めない** ことを示し、
prompt 強化路線の限界を示した。

## 影響

- skill_improver chain が **常に崩壊**: workspace 未作成 → eval cascade 失敗 →
  改善案出力なし。 skill_improver の機能が事実上死亡
- nested run_skill chain の信頼性検証が batch 4 では完遂できなかった
- LLM-driven な決定論処理の脆さを露呈、 G2 (preprocessor 化) の motivation
  evidence

## 修正

### 第 1 弾 (`d9787cb`、 batch 4 fix wave) — partial fix

- `max_act_turns: 3 → 6` (= LLM に余裕を持たせる)
- glob pattern instruction で `<original_dsl_root>` prefix 強制 + parent
  directory glob 禁止注意明示
- 3 Tier 2 test (`tests/test_copy_to_work_phase.py`) 追加: phase definition の
  invariant pin

これは **延命策**。 batch 4 retest では workspace 作成 ✅ 確認したが、 batch 5 で
LLM が再び glob constraint 違反 + write skip を再現 → root cause は LLM 関与
そのもの、 と認識を改めた。

### 第 2 弾 (`763c86c`、 [giveup G2](../../giveup-tracker.md) resolved) — 真の解

`copy_to_work` phase を **Phase Preprocessor** で書き換え、 LLM 完全廃止:

- `max_act_turns: 6 → 0` (= LLM act loop 廃止)
- `allowed_ops: [file] → []` (= decide-only phase)
- 8 step deterministic chain (python ×4 + run_op glob ×2 + iterate ×2):
  1. `compute_paths` (python): path derivation
  2-3. `file/glob` ×2: skill.md + phases/*.md
  4. `build_copy_plan` (python): glob 結果結合 + eval.md 除外
  5. `iterate file/read`: 各 source を read
  6. `build_write_ops` (python): read 結果と dst path を pair
  7. `iterate file/write`: 各 dst に書き込み
  8. `validate_copy` (python): files_written == files_expected を検証

LLM call 数: 旧 3-6 turns → 新 **0** (cost ≈ 0 for copy phase、 月 $0.01-0.05/run の削減)。

新 Tier 2 test 6 件 (`tests/test_copy_to_work_preprocessor.py`、 sibling skill
非汚染 invariant も pin = B4-L1 を構造的に保証)。

## 後続 (= batch 5 retest 2 で部分 verify)

batch 5 retest 2 は G2 fix (`763c86c`) **landing 前に走った** (= 並列 fix +
retest の HEAD 整合性問題)。 観測された B5R2-H2 (= copy_to_work が 0-byte
file 書く) は LLM-driven 版の最後の症状で、 G2 preprocessor 化で構造的解消
見込み。 batch 6 で post-G2 retest が必要。

## 教訓

1. **LLM-driven な決定論処理は脆い**: budget 拡大 + instruction 強化は
   延命策、 真の解は LLM を排除する preprocessor 化 (= memory
   `feedback_deterministic_split.md` で formalize)
2. **「3 turn で足りない」 vs 「LLM がそもそも要らない」 の判断境界**:
   determinable な処理 (= path / list / template) は LLM 不要、 budget 拡大は
   答えにならない
3. **partial fix と真の解の関係**: `d9787cb` は B4-L1 (glob over-broad) も
   同時 fix した。 partial fix で時間を稼ぎつつ真の解を G2 で landing する
   2 段階方式は production-grade 開発で受容可能、 ただし「partial fix で
   止めない」 ことを tracker で管理 (= G2 の Status を「resolved」 に上げる
   までを 1 unit of work とする)
4. **`run_skill` 階層の counting 単位は phase 境界**: `judge_phase` は
   `eval.evaluate` の iterate preprocessor として走り、 run_skill 層では
   ない (= P1-P8 整合)。 docs に明記すべき
