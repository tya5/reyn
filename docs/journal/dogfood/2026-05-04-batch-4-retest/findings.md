# Batch 4 — Findings

> B3 fix の retest (S1+S2) + 新規 nested skill chain 観測 (skill_improver 経由)。
> main HEAD `dee6ce4`、 2026-05-04。

## 概要

| ID | 重要度 | 一行で言うと | 状態 |
|---|---|---|---|
| [B4-H1](findings/B4-retest-S1-S2.md) | HIGH | `_run_skill_awaitable` が narrator 結果を private `_put_outbox` 経由で push → `_router_loop_agent_replies` に捕捉されず specialist reply が user に届かない | **fixed** at `ffc9b4a` |
| [B4-H2](findings/B4-S3-skill-improver-nested.md) | HIGH | `copy_to_work` skill の `max_act_turns: 3` overflow → workspace dir 未作成 → eval cascade が score 0.0 で失敗 (= skill_improver 改善 chain が常に崩壊) | **partial fix** at `d9787cb` (3→6 + glob scope) → batch 5 で write skip 再現 → 真の解 = preprocessor 化、 tracked as **G2** in [giveup-tracker](../giveup-tracker.md) |
| [B4-M1](findings/B4-S3-skill-improver-nested.md) | MED | `eval.md` path mismatch: `eval_builder` が `reyn/local/<slug>/eval.md` に書き、 `prepare` は `<target_dsl_root>/eval.md` を先に探す → 4 回 failed read 後に正しい path | open |
| [B4-L1](findings/B4-S3-skill-improver-nested.md) | LOW | `copy_to_work` が `src/reyn/stdlib/skills/**/*.md` で全 39 skill を glob (target slug のみすべき) → token 浪費 | **fixed** at `d9787cb` (`<original_dsl_root>` prefix 強制) |
| [B4-INFO-A](findings/B4-S3-skill-improver-nested.md) | INFO | nested skill 階層は **3 layer** (skill_improver → eval_builder/eval → direct_llm)、 `judge_phase` は iterate preprocessor で run_skill 層ではない | resolved |
| [B4-INFO-B](findings/B4-S3-skill-improver-nested.md) | INFO | `run_skill_started` event に `parent_run_id` 欠如 → R-D13 (nested tree display) の motivation evidence | resolved |
| [B3-H1 retest](findings/B4-retest-S1-S2.md) | HIGH | `48676ad` fix 効果検証 — specialist 側で `list_skills → invoke_skill` 経路到達 ✅、 ただし B4-H1 で reply が user に届かない | **fix verified** (invoke 到達)、 別 layer で blocked |
| [B3-M2 retest](findings/B4-retest-S1-S2.md) | MED | router が `read_local_files` 明示でも tool 呼ばない問題は LLM 分散あり (Trial A / B 異挙動)、 ask_user は依然 dark | partial — root cause が catalog 反映 timing 等の別問題 |

**新規 HIGH 2 件** (B4-H1 / B4-H2)、 MED 1 件 (B4-M1)、 LOW 1 件 (B4-L1)、 INFO 2 件 resolved。

## ハイライト narrative

### B3-H1 fix 効果と「最後の 1 cm」 問題 (B4-H1)

batch 3 で発見された B3-H1 (specialist `list_skills → stop`) を `48676ad` で修正
(router_system_prompt.py に「list_skills 後 invoke or describe 必須」 ルール追加)。
batch 4 retest で specialist 側の挙動を再観測:

- ✅ specialist RouterLoop が `list_skills` → `invoke_skill("direct_llm")` まで到達
- ✅ `direct_llm` skill がカレーレシピを完全生成
- ✅ `skill_narrator` が `reply_text` を生成

**しかし user にはレシピが届かなかった**。 deep dive で root cause 判明:

`src/reyn/chat/session.py` の `_run_skill_awaitable()` が narrator の reply_text を
**private `_put_outbox`** で送出しているため、 RouterLoop が監視している
`_router_loop_agent_replies` collection に **捕捉されない**。 RouterLoop 終了時に
`agent_replies` が空 → default agent は B2-H2 fix path で「specialist から処理結果が
得られませんでした」 を user に送る、 という挙動。

つまり **B2-H2 (silent absorption fix) が今度は誤検知になっている**: skill output は
完全に生成されているのに、 outbox 経由が違うので RouterLoop には見えない。

修正方針 (B4-H1 の next action):
- `_put_outbox` を public `put_outbox` に変更し、 `_run_skill_awaitable` が
  RouterLoop の `agent_replies` も update するように
- `_append_history` の二重呼び出し回避は要確認

### nested skill chain は 3 layer + cascade 失敗 (B4-H2)

B3-S3 の setup 問題後、 `skill_improver` を入り口に 3-4 階層 nested chain を狙う
B4-S3 を実行。 観測階層は **3 layer**:

```
L1: skill_improver
  L2a: eval_builder    (prepare phase で起動)
  L2b: eval            (run_and_eval phase で起動)
    L3: direct_llm     (eval.run_target で起動 — エラーで終了)
```

`judge_phase` は L4 ではなく `eval.evaluate` の **iterate preprocessor** として走る。
これは設計通りで P1-P8 整合 (= run_skill IR op は phase 境界、 preprocessor は phase 内部の hook)。

ただし **eval cascade が score 0.0 で崩壊**。 root cause は `copy_to_work` skill の
`max_act_turns: 3` 制約: LLM が glob + redundant read で 3 turn 消費し、 write step
に到達しないまま強制 finish。 結果 `.reyn/skill_improver_work/direct_llm/` が
作成されず、 後続の `eval.run_target` が `[Errno 2] No such file` で失敗。
fail を受けた eval が score 0.0、 改善案は出ず、 `skill_improver` chain が無効化。

**修正方針** (B4-H2 の next action):
- `copy_to_work` の `max_act_turns` を `3` → `5-6` に増やす、 もしくは
- `copy_to_work` を Phase Preprocessor (`run_op` ベース) に書き換えて LLM 不要化

副次 finding (B4-L1): `copy_to_work` の glob pattern が `src/reyn/stdlib/skills/**/*.md`
で全 39 skill を引っ張る。 target slug のみ glob すれば token 9 割削減見込み。

`run_skill_started` event に `parent_run_id` が無く、 階層関係は top-level JSONL の
co-location でしか復元できない (B4-INFO-B)。 R-D13 (nested tree display) の
motivation evidence になる。 cost は $0.018 / 192K tokens (3 skill run)。

### B3-M2 retest: ask_user は依然 dark

S2 では 2 trial 実行、 挙動分散:
- Trial A: B3-H1 fix の効果で `list_skills` を呼んだが、 catalog に
  `read_local_files` が含まれず中断。 MCP server 初期化 timing の問題と推定
- Trial B: router LLM が tool を呼ばず direct text reply (B2-M1 family の再発)

`intervention_dispatched` は両 trial で 0 件。 `ask_user` IR op 経路は依然 dark。
B3-M2 は B3-H1 fix では十分解消されておらず、 catalog 反映 timing or LLM
attractor の別 root cause がある。 batch 5 で focus すべき領域。

## 事前 prediction の精度

- **S1 「70% でカレー届く」**: 外れ — fix 動作したが新 layer (B4-H1) で blocked
- **S2 「40% で ask_user 観測」**: 当たり — 「外れ予測 = router が pre-skill clarification」 が trial B で的中

精度 1/2。 batch 3 の 4/5 から低下したが、 これは「fix 後の動作確認」 という
batch 4 の特性上、 prediction は high (70%) 寄りに振れていた。 「fix が効いた領域は
別 layer で再発する」 という batch 3 の教訓 (= batch 4 の predicting outline で
明示) が S1 で命中している (= attractor → outbox layer の attractor へ移行)。

## 次のアクション

1. **B4-H1 fix** — `_put_outbox` → `put_outbox` 公開化 + `_run_skill_awaitable`
   が `agent_replies` collection を update するよう調整。 `_append_history`
   二重呼び出し回避の test 追加。 batch 4 で fix 後 S1 を 3 度目の retest。
2. **B4-H2 fix** — `copy_to_work` の `max_act_turns` 緩和 (= preprocessor 化が
   理想だが MVP は constant 増加) + `copy_to_work` 単体テスト追加。
3. **B4-M1 fix** — `eval.md` の path 探索順序を統一、 `eval_builder` の write 先と
   `prepare` の read 先を一致させる。
4. **B3-M2 root cause** — MCP catalog 反映 timing の調査 (= batch 5 設計に組み込む)。
5. **batch 5 設計** — B4-H1/H2 fix 後の S1 + skill_improver retest、 ask_user
   経路を強制的に通す scenario (= MCP 完全 init 待ち + path resolution 失敗誘発)。
