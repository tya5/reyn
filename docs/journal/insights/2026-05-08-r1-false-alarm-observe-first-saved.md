---
title: 「25/25 refuted」 false alarm — plumbing fix + observe-first で投機的 description rewrite を回避
discovered: 2026-05-08
session-context: batch 16 (= 2026-05-08 plan-mode validation、 5 scenario × N=5 = 25 runs) で全 25 が `refuted` 判定、 「R1 attractor が真因」 と一旦結論 → batch 17 で description rewrite trial を計画。 user feedback で observe-first を再適用、 plumbing artifact + driver-induced と判明、 真の R1 rate は 0/25
related-commits:
  - 3a59d8c  # G27 plumbing fix (= ChatSession history + A2A wait + parent_chain_id)
  - 61949df  # G28 driver-induced tracker entry
  - 1fc3925  # G24 retest update (= 真の R1 rate 0/25 認定)
related-giveup: [G24, G27, G28]
related-memory: [feedback_observe_before_speculate_llm, feedback_minimize_speculation, feedback_per_scenario_attractor_audit, feedback_verify_reproduce_first]
status: stable
---

# 「25/25 refuted」 という signal は真の attractor を示していなかった

> plumbing fix + observe-first discipline で「投機的 description rewrite」 を回避した話

## TL;DR

batch 16 (= plan-mode validation、 N=25) で **全 25 runs が `refuted` 判定**、
retro 結論「R1 attractor (= LLM が plan tool を 25/25 で拒否) が真因、 batch
17 で description rewrite trial」。 user push back「attractor については過去
の経験を生かしてください」 で observe-first discipline を再適用、 LLM trace
dump を含む観測 stack 再起動 → **plan IS invoked、 driver の verdict logic
が events log を読まずに reply text 表面を見ていた** ことが判明。

3-layer plumbing gap (= ChatSession history 不在 + A2A wait_for_running_plans
不在 + parent_chain_id 不在) を G27 で fix、 retest で **真の R1 rate = 0/25
(= 0%)**。 残 2/25 empty は別 attractor (= driver-induced G12 Pattern E、 G28
で driver-only 認定)。 batch 17 description rewrite wave は **ROI ゼロ** で
中止。

## 教訓 headline

1. **数字と原因の混同**: 「25/25 refuted」 ≠ 「LLM が tool を 25/25 拒否」。
   verdict の意味は「driver が refuted classify した」 で、 LLM behavior 観測
   data が無い状態は wide range の cause を含み得る。
2. **plumbing artifact vs LLM attractor の分離**: 観測経路が壊れている時に
   「LLM 挙動の問題」 と誤判定する pattern。 fix dispatch 前に **plumbing
   sanity probe with trace dump** で観測経路の生存を確認。
3. **observe-first discipline の真価**: trace dump (= REYN_LLM_TRACE_DUMP +
   dogfood_trace) なしに「LLM 拒否 vs plumbing 問題」 を分離不能。 推測 stack
   は無限大 possibility を生成、 観測は確定情報を生成。
4. **dogfood driver も観測対象**: 「production behavior の代理」 として
   driver を信じる前に、 driver 自身の行動を trace で確認。
5. **calibration recovery via re-measurement**: 1 batch の Brier 値を milestone
   化する前に、 N≥5 stability + plumbing sanity check が必要。

## Background — batch 16 retro 元結論

batch 16 (= 2026-05-08) で plan-mode validation を実施:

- 5 scenario × N=5 = 25 runs
- 全 25 runs が `refuted` 判定
- driver の verdict logic = 「`no plan events` in plan_summary」 = 「events log
  に plan_started など見えなければ refuted」

retro 元結論 (= batch 16 retrospective.md 元 version):

> 「LLM が plan tool を 25/25 で拒否」、 next wave (= batch 17) で plan tool
> description rewrite trial、 R1 attractor が真因と認定

= 1 batch の数字 (= 25/25) を「LLM behavior signal」 と即座に解釈。

## ステップ-by-ステップ narrative

### ステップ 1 — batch 16 retro 終了、 batch 17 計画

retro 結論「R1 attractor 真因、 description rewrite trial で 25/25 → 0-30%
empty 目標」。 batch 17 で SP rewrite + N=25 retest を dispatch する直前。

### ステップ 2 — user feedback「attractor については過去の経験を生かして」

memory `feedback_observe_before_speculate_llm.md` (= LLM への送信 payload を
観測する infra を整える前に推測を積み上げない) を再 surface。 同 memory の
RETRO-H1〜H4 では「推測 4 件中 1.5 件が観測で訂正された」 実績があった。

### ステップ 3 — observe-first discipline 再適用

LLM trace dump を含む全観測 stack を再起動:
- `REYN_LLM_TRACE_DUMP=1` で provider call の messages/tools/response を 1
  call 毎 file dump
- `dogfood_trace` で scenario run 中の events log を full snapshot
- `llm_replay` で deterministic re-execute

### ステップ 4 — 観測で「plan IS invoked」 が判明

trace dump 確認:

```json
{"role": "assistant", "tool_calls": [{"name": "plan", ...}], ...}
```

= LLM は plan tool を実際に呼んでいる。 events log 直接 grep:

```
plan_started step_count=3
plan_step_started name=read_a
plan_step_completed name=read_a
...
```

= plan は走っている。 batch 16 driver の verdict 「no plan events」 は **events
log を読まずに reply text 表面 (= "I'll read both files and compare...") を
見ていた**。

### ステップ 5 — 3-layer plumbing gap 特定

deep dive で観測経路の壊れ方を 3 layer に分離:

1. **ChatSession history 不在**: child plan step の reply が outer session の
   history に append されず、 driver が history snapshot で「reply 不在」 と
   誤判定
2. **A2A `wait_for_running_plans` 不在**: child plan が完了前に outer reply
   が returned、 final synthesis step の text が driver snapshot 時点で未
   capture
3. **`parent_chain_id` 不在**: 複数 plan run が同 events log で混在、 verdict
   logic が「どの run のどの plan か」 を区別不能

= 3 件全て **driver / plumbing 問題**、 LLM 挙動とは無関係。

### ステップ 6 — G27 fix landing → sanity probe

3 layer fix (= commit `3a59d8c`) を land 後、 1 simple prompt で sanity probe:

| metric | pre-G27 | post-G27 |
|---|---|---|
| reply length | 0 chars | 1010 chars |
| events log | plan_started 見えるが driver snapshot に reflect されず | full snapshot reflect |

= plumbing 経路が working である base 確認。

### ステップ 7 — retest で 0/25 R1 = 真の rate 0%

同 5 scenario × N=5 retest:

| metric | batch 16 元 | retest (post-G27) |
|---|---|---|
| `refuted` total | 25/25 | 0/25 |
| true `R1 attractor` (= LLM が plan を拒否) | 25/25 と判断 | **0/25** |
| empty residue | (= 観測不能) | 2/25 (= 別 attractor) |

= R1 attractor の真の rate は **0%**。 残 2/25 empty は **driver-induced G12
Pattern E** (= driver の特定 invocation pattern が post-tool empty-stop を
trigger) と判明、 G28 tracker entry で driver-only 認定。

### ステップ 8 — batch 17 wave 中止

「真の R1 rate = 0%、 改善余地なし」 で description rewrite trial の ROI は
ゼロ。 wave 中止、 G24 retest update commit (= `1fc3925`) で「R1 attractor
は plumbing artifact、 真の rate 0/25」 と確定記録。

## Methodology — 「次にこの pattern を踏まないために」

1 batch の dogfood 結果が「LLM 挙動」 を示唆する時、 fix wave 進行前に **必
ず以下を確認**:

- [ ] LLM trace dump で actual request/response の記録あり?
- [ ] driver の verdict logic は events / WAL を直接読んでいる? それとも
      reply text 表面を見ている?
- [ ] batch 元 spec 通りに data flow が working していると **plumbing sanity
      probe** で確認したか? (= 1 simple prompt で expected reply が returned
      されるか確認)
- [ ] verdict 自体に bug の可能性は? (= S3 / S5 のような driver / scenario
      design 問題)

これら 4 件のいずれか 1 つでも No なら、 fix wave に進む前に **observation
infra の再整備** を最優先。 「数字に踊らされる trap」 の variant として:

- variant 1: 計測経路依存 (= 同 scenario でも subprocess vs programmatic で
  乖離。 詳細は [envelope-layer-attractor-fix](2026-05-07-envelope-layer-attractor-fix.md))
- variant 2: 動詞依存 attractor を 1/N noise に紛れ込ませる (= 詳細は memory
  `feedback_per_scenario_attractor_audit.md`)
- **variant 3 (= 本 insight)**: plumbing 問題を LLM 挙動 problem と誤判定、
  driver / verdict logic 自体を観測対象から除外している pattern

## Universal pattern — 4 メタ原則 (= 4 メタ memory) との対応

本 case が踏みかけた / 救った 原則:

| 原則 | 踏みかけた? | 救った行動 |
|---|---|---|
| `feedback_minimize_speculation.md` (= 1 仮説 1 修正 1 検証) | ◯ | batch 16 retro が「R1 attractor 真因 + description rewrite」 を bundle 仮説で固定。 observe-first で「真因が plumbing」 と単一仮説単位で訂正 |
| `feedback_observe_before_speculate_llm.md` (= LLM 観測 infra 先) | ◯ | LLM trace dump を含む観測 stack 再起動が「plan IS invoked」 確定の唯一手段 |
| `feedback_verify_reproduce_first.md` (= verify-first / reproduce-first) | ◯ | 「真の R1 attractor が現 HEAD で再現するか」 を観測前に推測しなかったら batch 17 全体が空回り |
| `feedback_per_scenario_attractor_audit.md` (= 1/N noise の cross-scenario rate matrix) | △ | 残 2/25 empty を「subprocess + minor mistake」 で dismiss しかけたが、 G28 tracker で driver-induced と分離 |

## Future work — 真の measure は再測定で確定

本 insight で **claim していない** 事:

- 真の R1 attractor rate が「0%」 で恒久確定: **future wave (= G27 plumbing
  以外の path で plan が呼ばれない条件) で再測定 mandatory**
- batch 16 の Brier 0.83 (= 元計算) が真の calibration: **真の Brier は
  retest data (= 0/25 R1) に基づく recompute で別値 (= 後続 batch で確定)**
- G28 が driver-only で恒久確定: **driver fix 後に N≥5 で empty rate 0% 確認
  までは provisional 認定**

「投機的 description rewrite を回避した」 だけで本 insight の主張は閉じ、
それ以上の measure claim は future re-measurement の prerequisite。

## References

### dogfood / giveup
- batch 16 元 retro + 末尾 G27 retest addendum: `docs/journal/dogfood/2026-05-08-batch-16-plan-mode-validation/retrospective.md`
- giveup-tracker `G24` (= retest 結果反映済): `docs/journal/dogfood/giveup-tracker.md`
- giveup-tracker `G27` (= plumbing fix 3-layer): 同上
- giveup-tracker `G28` (= driver-induced G12 Pattern E): 同上

### memory pointer (= 次 session の自分向け)
- `feedback_observe_before_speculate_llm.md` (= 観測 infra 先)
- `feedback_minimize_speculation.md` (= 1 仮説 1 修正)
- `feedback_per_scenario_attractor_audit.md` (= 数字に踊らされる trap variant 2)
- `feedback_verify_reproduce_first.md` (= reproduce-first)

### concept / process
- 9 原則 framework: `docs/en/contributing/dogfood-discipline.md`
- 関連 insight: [envelope-layer-attractor-fix](2026-05-07-envelope-layer-attractor-fix.md) (= 数字に踊らされる variant 1)
- 関連 insight: [plan-mode-dogfood-findings](2026-05-07-plan-mode-dogfood-findings.md) (= 同 plan-mode 系列の前 insight、 1/10 noise が 25% attractor を隠した case)
