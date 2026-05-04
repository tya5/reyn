# Batch 9 (B8-NEW fix wave) — Findings

> batch 8 で確定した 3 HIGH bug (G15 / G16 / G17) を fix dispatch + per-fix retest した
> fix batch。 **G15 ✅ 真に effective、 G17 ❌ wrong layer (test 通っているが runtime 構造と
> 不一致)、 G16 ❌ no observable effect (= weak LLM が wording 差を読まない)**。
> 加えて **新 blocker 3 件 (B9-NEW-1/2/3) 発見** で次 wave 候補が確定。

## Summary table

### Fix landing

| Fix | Commit | Test 件数 | Test 結果 | e2e effect |
|---|---|---|---|---|
| **G15** (B8-NEW-3) | `651a053` | 7 Tier 2 | ✅ | ✅ **真に effective** — stdlib file reads が permission_denied 解消、 chain が analyze_skill 通過 |
| **G17** (B8-NEW-6) | `d1f2d30` | 5 Tier 2 | ✅ | ❌ **wrong layer** — test fixture が `{"type":"unknown","data":{"target_skill":...}}` を assume したが runtime は `{"eval_spec":...,"target_skill":...}` (data wrapper 無し) で生成、 同 ValueError 継続 |
| **G16** (B8-NEW-5) | `330dd2a` | 9 Tier 1 | ✅ | ❌ **no observable effect** — weak LLM が description の wording 差 (Build vs Evaluate) を読まず、 router が引き続き `eval` skill を誤誘導 |

### Post-fix retest verdicts (= per-scenario)

| Scenario | B8 baseline | B9 verdict | 主要発見 |
|---|---|---|---|
| [S1](findings/B9-S1-retest.md) chain 完走 | blocked | **inconclusive** | G15 ✅ で analyze_skill 通過、 chain が **write_eval 到達** (Reyn 史上初到達) → artifact validation で失敗 = B9-NEW-1 |
| [S5a](findings/B9-S5a-retest.md) 自然言語 invoke | refuted | **refuted (継続)** | G16 wording 効果無し、 router が依然 `eval` skill を選択。 description の `Build` 動詞も list 表示で短く切られて differentiation 失敗 |
| [S5b](findings/B9-S5b-retest.md) 構造 invoke | refuted | **refuted (継続)** | G17 fix wrong layer で同 ValueError、 test は通るが runtime artifact 構造と不一致 = B9-NEW-2 |

### 検出した新 bug (= batch 10 fix 候補)

| ID | 重要度 | 場所 | 内容 |
|---|---|---|---|
| **B9-NEW-1** | HIGH | `eval_builder/phases/write_eval.md` or output schema | write_eval phase が `eval_spec_result` schema validation で 3 attempt 全失敗。 LLM 出力 artifact が schema 不適合 (= 必須 field 欠落 or 型不整合)。 chain 完走の primary blocker (= G15 で unblock した chain の次 layer) |
| **B9-NEW-2** | HIGH | `eval_builder/analyze_skill_resolver.py:_extract_skill_name` | G17 fix が `data["target_skill"]` を check するが runtime artifact は `artifact["target_skill"]` (top-level)。 test fixture と runtime が乖離、 fix 効果ゼロ |
| **B9-NEW-3** | MED | router loop / G3 dedupe 系 | `run_skill` 失敗後に router が `invoke_skill(skill_improver)` を duplicate invoke (= S1 で T+141s/T+147s/T+157s)。 G3 dedupe の symmetric 拡張候補 |

### Cost summary

| Session | Tokens | Cost USD |
|---|---|---|
| S1 retest (3 turns + retry) | 65,882 | $0.001891 |
| S5a retest | ~32,000 | ~$0.001246 |
| S5b retest (3 attempts) | ~13,500 | ~$0.001700 |
| **Total** | ~111,000 | **$0.003705** |

= per-fix retest で 1 セント未満。 weak LLM cost-effectiveness 引き続き。

## Round 別 narrative

### Round 1: prelude + 3 fix dispatch

main HEAD `e78d7b2` に prelude landing 後、 sonnet × 3 で並列 fix dispatch:
- R1 (G15): 詳細診断 + Hyp A + Hyp B 確認 → 2 root cause 同時 fix (`651a053`)
- R2 (G17): field-presence-first inversion fix (`d1f2d30`)
- R3 (G16): description distinctive verb + when_not_to_use 拡張 + symmetric eval fix (`330dd2a`)

3 fix 並列 landing で test 1000 passed (+21 from 979 baseline)、 0 regression。

### Round 2: post-fix retest sub-wave

main HEAD `330dd2a` (= 3 fix 全 landing 後) で sonnet 1 体に S1+S5a+S5b の sequential
retest dispatch。

実行結果:
- S1: G15 ✅ 確認 (= permission_denied 解消) 、 ただし write_eval で新 blocker
- S5a: G16 効果無し
- S5b: G17 wrong layer 露呈

### Round 3: G17 wrong layer の構造分析

`tests/test_eval_builder_path_resolution.py` の test fixture:
```python
artifact = {"type": "unknown", "data": {"target_skill": "direct_llm"}}
```

OS が runtime で生成する artifact (= S5b retest 観測):
```python
{"eval_spec": {"name": "direct_llm.md"}, "target_skill": "direct_llm"}
```

= **test fixture が artifact wrapper 構造を仮定 (`data` key)、 ただし OS の実 invocation
は wrapper 無しで top-level に field 並べる**。 G17 fix は `data["target_skill"]` を
check するが runtime artifact では空 dict、 旧 user_message regex fallback に落ちて
ValueError。 **test 通過 + runtime 失敗** という最 dangerous な「wrong layer test」 trap。

教訓: **Tier 2 OS invariant test は fixture が「OS 実生成 artifact 構造」 と一致する
ことを Tier 3 LLMReplay 等で cross-check するべき**。 同等 test fixture が他 fix で
未検出のまま積まれている可能性あり、 monitor 候補。

### Round 4: G16 wording fix の no-effect 観測

router system prompt の `## Available skills` inline 一覧での description (= G12
truncation で 80 chars cap):

```
- eval: Evaluate a target skill against a single test case using judge_phase as L...
- eval_builder: Build an eval spec (eval.md) — to run evaluations use the eval s...
```

両 skill とも 80 chars 直前で truncate される。 `eval_builder` の `to run evaluations
use the eval skill instead` という contrast 部分が **truncate 後にも残ってはいる** が、
weak LLM (gemini-2.5-flash-lite) はこの distinction を「読む」 だけの attention を
持たず、 user input の `eval` keyword に anchor して `describe_skill(eval)` →
`invoke_skill(eval)` に進む。

= **「skill 設計者が wording で disambiguate する」 path が weak LLM 環境では成立しない**
ことの実証。 真の解は (a) 強モデル併用 (= G4) もしくは (b) router system prompt 構造
変更 (= description でなく explicit decision tree) のいずれか。

## Prediction calibration

batch 9 prelude で予測:

| Sub-wave | Top prediction | Actual | Hit? |
|---|---|---|---|
| G15 単独 → S1 retest | 25% verified / 50% blocked | inconclusive | near-hit (= verified に半歩) |
| G15 + G17 → S5b retest | 35% verified / 35% blocked | refuted | miss (refuted = 20%) |
| G15 + G16 → S5a retest | 30% verified / 25% blocked / 35% refuted | refuted | **hit** (refuted top) |
| 全 fix → 統合 retest | 15% verified / 50% blocked | (実施せず) | n/a |

= 1/3 hit、 1/3 near-hit、 1/3 miss。 Brier ≈ 0.55、 batch 8 (≈ 0.96) より改善 ✅。

batch 8 retro 教訓 (= 累積 fix verify scenario の verified を 20-30%、 blocked + refuted
を 50% 以上) を反映した結果、 calibration が改善。 ただし「fix が test 通っても e2e で
効くとは限らない」 という批判層がある (= G17/G16 wrong layer / no-effect)。

## A4 review (= user 感覚との差分)

- **G15 fix の primary 効果は確認** ✅ (= chain progress significantly + new layer 露呈)、
  これは batch 9 の核心成果。 次 layer (write_eval) は B9-NEW-1 として batch 10 候補
- **G17 fix は test 通過 + e2e 失敗の trap**: 「test fixture と OS runtime artifact 構造が
  乖離」 という観測駆動でないと発見不能な bug 種別。 Tier 2 + Tier 3 cross-check の
  importance が dogfood で実証
- **G16 fix の no-effect は wording-fix 路線の限界**: weak LLM 環境で「skill description の
  contrast wording」 で routing 改善する path は成立しない、 構造的解 (= router system
  prompt の decision logic 化) が必要。 G4 trigger (= 強モデル併用) との合流が現実的解
- **prediction calibration は batch 8 → batch 9 で改善** (Brier 0.96 → 0.55)、 batch 8
  retro の教訓が機能
- **観測 infra は引き続き reliable**: sonnet 1 体の sequential retest で 3 scenario
  全部の verdict + 新 bug 構造を確定可能、 道具 ROI が batch 7-9 通じて確認

## 残懸念点 + 次 wave (= batch 10) 候補

| 優先 | 内容 | 関連 finding |
|---|---|---|
| **CRITICAL** | B9-NEW-2 fix: G17 wrong layer 修正 (= `_extract_skill_name` で `artifact["target_skill"]` top-level check 追加 + Tier 3 LLMReplay で runtime 構造と一致確認) | S5b |
| HIGH | B9-NEW-1 fix: write_eval phase の `eval_spec_result` schema 適合化 (= LLM 出力 wrong shape の調査 + schema or instruction fix) | S1 |
| MED | B9-NEW-3 follow-up: router invoke_skill duplication after run_skill failure (G3 dedupe 系) | S1 |
| MED | G16 follow-up: wording fix の限界実証 → 構造的 router decision logic 化を別 ADR で議論、 G4 trigger で同時解消する path も検討 | S5a |
| LOW | Tier 2 fixture audit: 「test fixture が runtime artifact 構造と乖離」 trap が他 fix にも潜在する可能性、 systematic audit 候補 | meta |

batch 10 は **B9-NEW-2 + B9-NEW-1 fix wave** が中心、 副次的に B9-NEW-3 (G3 拡張)。
G16 follow-up は wording-only 路線でなく構造的 router 設計に重みがある。

## 一言で

> **fix が test を通過しても e2e で効くとは限らない — Tier 2 fixture が runtime 構造と
> 一致することを Tier 3 cross-check しないと wrong layer trap が発生する**

— G15 ✅ 真に effective (= 観測駆動の正確な diagnosis + 2 root cause 同時 fix)
— G17 ❌ wrong layer trap (= test fixture と runtime artifact 構造の乖離)
— G16 ❌ no-effect (= weak LLM での wording-based disambiguation の限界実証)

batch 9 で Reyn の **「fix verify は per-fix Tier 3 e2e cross-check が必須」** という
testing policy 上の重要教訓が data で確定した batch。
