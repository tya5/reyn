# Batch 9 (B8-NEW fix wave) — Retrospective

> 3 fix を sonnet 並列 dispatch + 1 sonnet sequential retest で **fix batch** として実行。
> G15 が真に effective、 G17/G16 は test 通過しているが e2e で機能しない。 「fix verify は
> per-fix Tier 3 e2e cross-check が必須」 という testing policy 上の重要教訓を data で確定。

## 想定と現実のずれ

### 開始時の想定 (= prelude A1)

batch 8 で 3 HIGH bug の fix design は明確、 sonnet 並列で landing 後に sub-wave retest で
verify、 chain 完走候補もしくは次 layer blocker 露呈、 という想定。

### 実際の進行

| 想定 | 現実 |
|---|---|
| 3 fix dispatch → 並列 landing | ✅ 3 並列 sonnet で順次 landing (test 1000 passed) |
| G15 で chain 完走 candidates 観測 | ✅ chain が write_eval まで到達 (Reyn 史上初)、 ただし新 blocker (B9-NEW-1) で停止 |
| G17 で S5b 完走 candidates 観測 | ❌ test fixture と runtime 構造が乖離 (= B9-NEW-2)、 fix 効果ゼロ |
| G16 で S5a routing 改善観測 | ❌ wording 差を weak LLM が読まず、 router 引き続き `eval` 誤誘導 |
| Brier score batch 8 (0.96) より改善 | ✅ Brier ≈ 0.55、 calibration 改善 |

= 「fix が test 通れば e2e で効く」 は時に乖離する、 ことを batch 9 が data で実証。

## ターニングポイント 3 つ

### TP1: G15 fix の primary 効果確認 (= 真の structural fix)

batch 7 から 4 batch 連続「stdlib path read で chain が止まる」 問題が、 G15 fix で
真に解消した。 観測 evidence:
- B7-S1: copy_to_work で permission_denied 停止 (= 18 件)
- B8-S1: analyze_skill (eval_builder) で permission_denied 停止 (= 18 件、 earlier shift)
- B9-S1: analyze_skill 通過 ✅、 write_eval phase 到達 (Reyn 史上初)

G15 fix は **2 root cause を同時に diagnose して同時に fix した** のが成功理由:
- Hypothesis A: `startup_guard._prompt_file_access` が non-TTY で auto-fail
- Hypothesis B: `invoke_sub_skill` が `permission_resolver` を child に propagate しない

両方が成立しないと chain が動かない構造、 一方だけ fix しても変化しない。 sonnet R1 が
**code reading で 2 root cause を確定 + 1 commit で同時 fix** した judgment が決定的。

教訓: **batch 8 で観測した permission_denied は単一 root cause でなく 2 root cause の
合算だった**。 batch 8 の S1 finding で「B8-NEW-1 と同 root cause」 と書いたのは正確で
なかった、 構造的に「declaration あっても non-TTY で approval 機構が無い」 + 「sub-skill
が resolver を継承しない」 の 2 重 layer。

### TP2: G17 wrong layer の発見 (= test fixture と runtime の乖離)

G17 fix は Tier 2 test 5 件全 pass、 sonnet R2 の output report も「fix landing OK」 だった。
しかし B9-S5b retest で同 ValueError が発生。 詳細観測で判明:

```python
# Tier 2 test fixture (= 想定 artifact 構造)
{"type": "unknown", "data": {"target_skill": "direct_llm"}}

# OS runtime 実生成 (= S5b retest で観測)
{"eval_spec": {"name": "direct_llm.md"}, "target_skill": "direct_llm"}
```

= **test fixture が `data` wrapper を仮定しているが、 OS の実 invoke_skill 経路では
wrapper 無しで top-level に field 並べる**。 G17 fix は `data["target_skill"]` を check
するが runtime artifact では空 dict → 旧 fallback path → ValueError (= 同じ error)。

教訓: **「test 通過 + 実環境失敗」 は最 dangerous な fix trap**。 Tier 2 OS invariant
test は fixture が「OS が実際に生成する artifact 構造」 と一致することを Tier 3
LLMReplay で cross-check しないと、 wrong layer fix が test を通過してしまう。 同じ
trap が他 fix に潜在する可能性 (= meta bug、 batch 10 で systematic audit 候補)。

これは batch 7 の RETRO-H3 で観測した「推測スタックの自己強化 trap」 の Tier 2 版。
道具 (= 観測 infra) があれば 5 分で確定、 無ければ複数 batch 跨いで誤解継続するリスク。

### TP3: G16 の no-effect 観測 (= weak LLM environment での wording-fix の限界)

G16 fix は 9 Tier 1 contract test 全 pass で「skill.md 内 wording が想定通り変更された」
ことを確認。 ただし B9-S5a retest で router が引き続き `eval` skill を選択。

詳細観測:
- system prompt の inline 一覧で eval_builder description は `Build an eval spec
  (eval.md) — to run evaluations use the eval s...` (= 80 char truncation 後)
- weak LLM (gemini-2.5-flash-lite) は `eval を作って` の `eval` keyword に anchor、
  `describe_skill(eval)` → `invoke_skill(eval)` で誤誘導
- description の `Build` 動詞 / `to run evaluations use the eval skill instead`
  contrast を **読まない** (= weak LLM の attention budget 不足)

教訓: **「skill 設計者が wording で disambiguate する」 path は weak LLM 環境では構造的
に成立しない**。 真の解は:
- (a) 強モデル併用 (= G4 trigger 評価) で attention budget 確保
- (b) router system prompt 構造変更 (= description-based でなく explicit decision tree
  encoding)
- (c) skill 名空間で keyword competition を最小化する命名規則 (= eval_builder を
  spec_builder 等に rename)

(c) は backward-compat 影響大、 (b) は OS 構造変更で慎重、 (a) が現実的最短路。 G16 の
真の resolved は G4 trigger と合流するのが optimal。

これは care boundary framework (= structural / behavioral / gray の 3 区分) で言うと
「behavioral 路線 (= LLM 判断結果に依存) で patching したが効かない、 structural
(= LLM の判断 environment 整備) もしくは LLM capability 入れ替え (= G4) が必要」
という pattern。

## 観測 infra の reliable 利用 (継続)

batch 7 で整備、 batch 8 で第 2 回利用、 batch 9 で **fix batch 文脈での利用** という
位置づけ。 fix dispatch + retest sub-wave で reliable に機能:

| 道具 | batch 9 での utility |
|---|---|
| `dogfood_trace` | per-scenario chain / events / cost で per-fix verdict 確定 |
| `detect_attractor` | S5b で G12 attractor 1 件検出 (router 1st attempt) |
| `REYN_LLM_TRACE_DUMP` | per-session 別 path で multi-input dogfood (S1/S5a/S5b) を 1 sonnet 内で順次観測 |
| `llm_replay` | batch 9 では未使用、 ただし B9-NEW-2 / G16 follow-up での fix verify path 1 で活用候補 |

= 道具自体は完全に reliable。 batch 8 同様、 sonnet が自律的に組み合わせ。

## prediction 設計の教訓 (= batch 10 への継承)

batch 8 retro で確立した「累積 fix verify の verified base rate を 20-30%、 blocked +
refuted を 50% 以上」 calibration を batch 9 で適用、 Brier 0.96 → 0.55 に改善。

ただし新たな calibration 教訓:

| 種別 | batch 9 での観察 | batch 10 への継承 |
|---|---|---|
| structural fix (= G15 のような OS 層 root cause fix) | 真に effective、 verified or inconclusive (= 次 layer blocker 露呈) が typical | verified 35-45%、 inconclusive 25-35% に振る |
| layer fix (= G17 のような handler/resolver level の compensating fix) | test 通過 ≠ e2e effect、 wrong layer 確率 30%+ | verified 30-40%、 refuted 30-40% に振る (= test 通過しても期待値半分) |
| wording fix (= G16 のような prompt/description level fix) | weak LLM 環境では effective 確率 <20% | verified 10-20%、 refuted 50-60% に振る |

= **fix の「層」 で base rate が大きく異なる**。 structural > layer > wording の順で
verified 確率が下がる。 batch 10 prediction では fix 種別ごとに base rate を切り分け。

## チームダイナミクス (= user vs assistant)

batch 9 は user 介入が **3 箇所** あった、 batch 8 (= 0 箇所) と対照的:

| 介入 | 内容 | 効果 |
|---|---|---|
| TP1 (= 「skill author 契約として doc 化必要」) | G12 truncation の skill author contract 化を memo として記録 | 残件可視化、 batch 9 進行に影響なし |
| TP2 (= 「一緒に 80 をオプションで変更させることも残件として追加」) | `MAX_DESC_LEN_FOR_LISTING` の reyn.yaml override 化を残件 memo に追加 | 残件可視化 |
| TP3 (= giveup-tracker manual 更新 G15 → resolved) | sonnet R1 commit landing 後に user が手動で tracker 更新 | tracker と code 状態の同期確保 |

= user 介入が「**残件の可視化 + tracker と code の状態同期**」 として機能。 batch 7 で
TP として捉えた「設計再定義型介入」 (= 観測 infra 整備 / care boundary 言語化) とは
質が異なる、 **運用フェーズの介入** に shift。 batch が回るたびに user 介入の質が「設計
レベル → 運用レベル」 にシフトする傾向。

## 次 batch (= batch 10) への申し送り

### prediction 設計
- fix 種別 (structural / layer / wording) で base rate を切り分け
- structural: verified 35-45%、 layer: verified 30-40%、 wording: verified 10-20%

### Fix wave (= batch 10 で着手)

| 優先 | 内容 | scope |
|---|---|---|
| **CRITICAL** | B9-NEW-2 fix: G17 wrong layer 修正 (`_extract_skill_name` の top-level field check) + Tier 3 LLMReplay cross-check | 1 file edit + Tier 3 fixture |
| HIGH | B9-NEW-1 fix: write_eval phase artifact validation (= eval_spec_result schema or instruction fix) | scope 調査必要 |
| MED | B9-NEW-3 follow-up: router invoke duplication after run_skill failure (G3 dedupe 系拡張) | router_loop edit |
| 議論 | G16 follow-up: wording fix 限界の構造的解 (= router decision logic 化 ADR or G4 trigger 合流) | 別 ADR or 待ち |

### Meta audit
- **Tier 2 fixture audit**: G17 wrong layer trap が他 fix にも潜在する可能性、 systematic
  audit で fixture が runtime artifact 構造と一致するか確認 (= batch 10 中に並走 wave)

### 残課題 (= 引き続き defer / monitor)

- batch 8 で確認できなかった Option F UX を `llm_replay --patch` で synthetic verify
- describe_skill 強制 (= H2 第 2 層 input field hallucinate)、 別 wave で実装中
- proxy 強モデル追加 (user-side) → Wave 3 G4 spike (G16 真の解と合流)

### 設計原則の運用
- batch 7-9 で確立した 4 原則 (= deterministic split / minimize speculation / observe
  before speculate / care boundary) を継続運用
- **新原則候補**: **「fix verify は per-fix Tier 3 e2e cross-check 必須」** を testing.md
  に追加検討、 wrong layer trap 予防

## 一言で

> **G15 真に effective、 G17/G16 は test 通過 + e2e 失敗の wrong layer trap — fix の「層」
> で verified 確率が桁違いに変わることを batch 9 が data で実証**

— structural fix (G15) は真に効く、 chain が Reyn 史上初の layer (write_eval) に到達
— layer fix (G17) は test fixture と runtime artifact 構造の乖離で wrong layer trap
— wording fix (G16) は weak LLM 環境で no-effect、 真の解は構造的 / model 入れ替え

batch 9 で「fix verify には Tier 2 + Tier 3 cross-check が必須」 という testing
discipline 上の重要教訓が data で確定した batch。
