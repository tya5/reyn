# Batch 8 (cumulative fix verification) — Retrospective

> 観測 batch (= fix dispatch しない方針) として実行。 期待した「累積 fix の e2e
> 完走 verify」 は未達、 ただし **新 blocker 4 件の data-driven 発見** + **B8-NEW-2
> fix の e2e 初確認** + **router 1-turn 効率改善** という 3 つの構造的成果を得た
> 短〜中規模 batch。

## 想定と現実のずれ

### 開始時の想定 (= scenarios.md A1)

batch 7 wave で landing した 8 件の fix の **累積 e2e 効果** を chat 経由で
verify、 「chat 経由で skill_improver が動く前提が揃った」 を data 確認する batch。

| 想定 | 想定根拠 |
|---|---|
| S1 chain 完走 verify (45% verified) | 8 commit 累積で大半の blocker が解消されているはず |
| S2 Option F UX 確認 (40% verified) | batch 7 で 50% empty stop 観測、 同 rate なら simply trigger される |
| S3 H3 unblock (40% blocked) | preprocessor 化で LLM 経路が消え観測機会限定 |
| S4 truncation 構造的 verify (70% verified) | payload trace で確認可能、 ほぼ確実 |
| S5a 自然言語 invoke (65% verified) | router enum fix で hallucinate 消失するはず |
| S5b 構造データ invoke (55% verified) | preprocessor anyOf fix で union input 受理するはず |

### 実際の進行

| 想定 | 現実 |
|---|---|
| chain 完走 verify | **earlier shift** で停止 — B8-NEW-2 fix が露呈させた B8-NEW-3 で blocked |
| Option F trigger 確認 | **trigger 自体不在** — G12 truncation fix が trigger 消失させた可能性、 Option F UX 未到達 |
| H3 unblock | S1 同 blocker で blocked、 観測経路が引き続き不可 |
| truncation 構造的 verify | system prompt 経路 ✅、 ただし tool schema 経路は **非 truncate** = B8-NEW-4 |
| 自然言語 invoke | dot-notation 消失 ✅、 ただし `eval` skill 誤誘導 = B8-NEW-5 |
| 構造データ invoke | anyOf fix ✅、 ただし `_extract_skill_name` で ValueError = B8-NEW-6 |

= dogfood が **「fix landing は 1 layer 解消、 次 layer の新 blocker は >50% 確率で
露呈する」 という構造的性質** を data で実証した batch。

## ターニングポイント 3 つ

### TP1: S1 の earlier shift 観測

batch 7 fresh retest で `copy_to_work` で permission_denied 停止していた chain が、
batch 8 では **更に earlier の `analyze_skill`** で停止した。 一見後退に見えるが
実際は:

- B7-S1: `analyze_skill` 内で B8-NEW-2 (PureModeViolation) が silent abort →
  `copy_to_work` まで chain 進行 (見かけ上)、 そこで permission_denied
- B8-S1: B8-NEW-2 fix で `analyze_skill` が真に動き始める → 即 permission_denied で
  停止 (= B8-NEW-3 が露呈)

= **B7 では B8-NEW-2 が B8-NEW-3 を mask していた**。 batch 7 で「stdlib read
permission は skill_improver には付与した、 次は eval_builder」 が見過ごされて
いたのを、 B8-NEW-2 fix が initialise した。

教訓: **fix の数だけ blocker layer が露呈する**。 scenarios.md prediction で
chain scenario の verified を 45% にしたのは batch 7 教訓の生かしきれなさ
(= 累積 fix で verified 確率を過大評価 trap)。

### TP2: S5a の hallucination variant transformation

router enum fix (`9ee6ae1`) は B7-S5a の dot-notation hallucinate (`eval_builder.eval_md`)
を確実に排除した。 が、 batch 8 では **新 variant が出現**:

- B7-S5a: `invoke_skill(name="eval_builder.eval_md")` → ValueError (skill not found)
- B8-S5a: `invoke_skill(name="eval")` → eval skill が **実際に起動・完走**

= dot-notation hallucination は guard で即失敗していたが、 新 variant は
**既存 skill (= eval) への誤誘導** で表面上完走、 user 意図と完全に乖離した
silently wrong 動作。

これは **enum 制約 fix が「skill 名空間の境界」 は守れるが「user intent と skill
selection の意味合い」 は守れない** という structural 観察。 後者は eval_builder
の `when_to_use` wording / `description` 設計の問題。

教訓: **enum fix は naming consistency を確保するが routing intent ambiguity は
別 problem space**。 fix の effective scope を狭く正確に評価する必要。

### TP3: 観測 infra ROI の確認

batch 7 で投資した 4 道具 (`dogfood_trace` / `llm_replay` / `detect_attractor` /
`REYN_LLM_TRACE_DUMP`) が batch 8 で **reliable に機能した**:

- 2 sonnet が独立 worktree で並列実行、 各々が観測 infra で bug 構造を確定
- 例: B8-S5b では `python_step_failed` payload で「artifact_type=unknown,
  data has no 'text' field, regex fallback failure」 という連鎖が data で確定
- 道具なしでは「LLM が `eval` skill を選んだ理由」 「`compute_paths` 失敗の
  詳細」 等が推測スタック化していたはず

batch 7 で 1 day 投資した道具が batch 8 の 1 batch 全体で活用された = **investment
amortise が確認**。 batch 9 以降も同等の reliability 期待できる。

教訓: **観測 infra は 1 batch で投資、 N batch で回収**。 batch 7 retro で
「道具なし vs 道具あり」 比較した iteration speed 改善は batch 8 で実証。

## 観測 infra 利用の進化

batch 7 で初めて整備、 batch 8 で **第 2 回利用** という位置づけ。 batch 7 が
「整備 + retroactive 検証」 だったのに対し、 batch 8 は「**通常 dogfood の primary
道具として default 利用**」 のフェーズ:

| ツール | 使い方の進化 |
|---|---|
| `REYN_LLM_TRACE_DUMP` | per-session 別 path 切替 (`s5a.jsonl` / `s5b.jsonl`) で multi-input dogfood に対応 |
| `dogfood_trace` | sonnet が自律的に `--mode chain` / `--mode events` を組み合わせて使用 |
| `detect_attractor` | sonnet が自律 detection、 S5a で 1 件検出 (`stop_with_must_rule`) |
| `llm_replay` | 今回未使用、 batch 9 で B8-NEW-3 等の fix verify path 1 で活用予定 |

整備時の想定通り **「sonnet が自律的に観測道具を組み合わせて使う」** UX が
batch 8 で初めて確認された。 道具の self-explanatory さ + sonnet の judgment
が両方適切でないと成立しない、 これが回ったのは positive。

## prediction 設計の教訓 (= batch 9 への継承)

batch 8 で試行した 4 区分 prediction は batch 7 retro 教訓 (= `blocked`
カテゴリ追加) を反映したが、 **calibration が batch 7 baseline (≈0.45) より
悪化** (≈0.96)。

理由:
- 累積 fix verify scenario で「fix が積まれているなら verified が高い」 と
  直感的に予測
- 実際は **fix 1 件 = 1 layer 解消、 次 layer の new blocker が >50% 確率で露呈**
- = base rate 的に verified を 20-30% に抑え、 blocked + refuted を 50% 以上に
  振るのが正しい

新 calibration 指針 (= batch 9 で適用):

| Scenario 種別 | 推奨 verified base rate | 推奨 blocked + refuted base rate |
|---|---|---|
| 累積 fix の chain 完走 verify | 20-30% | 50-60% (blocked 30-40% + refuted 20-30%) |
| 単独 skill 直接 invoke retest | 30-40% | 40-50% |
| 構造的 fix の payload 確認 (= S4 型) | 60-70% | 20-30% (blocked 5-10% + inconclusive 15-20%) |
| Option F / 確率的 trigger 系 | 30-40% | 40-50% (= trigger 不在 blocked リスク) |

教訓: **「fix 後の verified を高く予測する」 は batch dogfood の典型 trap**。
fix は 1 layer の問題を解消するだけで、 e2e success は構造的 dependency chain の
全 layer green を要求する。

## チームダイナミクス (= user vs assistant)

batch 7 の TP1-3 (= 観測 infra dispatch / 過剰ケア指摘 / care boundary 言語化)
は user 介入が batch を再定義した。 batch 8 は対照的に **user 介入無しで推移**:

- A1 scenarios.md draft → A2 review (user "進めて" のみ) → A3 並列実行 → A4 finding
- 各 step が batch 7 で言語化された原則 (= 観測駆動 / care boundary / minimize
  speculation) で自律的に運用された
- = batch 7 で確立された原則が **assistant の internal practice として定着**

user 介入が無いことは batch 7 で築いた framework の robustness を意味する。
ただし「想定外の paradigm shift が起きにくい」 ともいえる。 batch 8 はその意味で
「言語化済原則の運用 batch」、 質的に新 insight は batch 7 ほどではない。

## 次 batch (= batch 9) への申し送り

### prediction 設計
- 累積 fix scenario の verified base rate を **20-30%** に下げる
- chain 系で `blocked + refuted` を **50% 以上** に振る
- 構造的 fix verify (= payload 確認系) は引き続き **60-70% verified**

### Fix wave (= batch 9 で着手)

| 優先 | 内容 | scope |
|---|---|---|
| **CRITICAL** | B8-NEW-3: eval_builder permissions に stdlib path read 許可追加 | 1 file edit + Tier 2 test |
| HIGH | B8-NEW-5: eval_builder の routing ambiguity 対策 (`when_to_use` 拡張 or example phrase) | skill.md edit + Tier 3 LLMReplay |
| HIGH | B8-NEW-6: `_extract_skill_name` の unknown artifact_type ハンドリング | resolver edit + Tier 2 test |
| MED | B8-NEW-4 follow-up: tool function descriptions truncation (Pattern A 保険) | router_tools.py edit |

### 残課題 (= batch 8 で着手しなかった項目、 batch 9+ 候補)

- B8 で確認できなかった Option F UX を `llm_replay --patch` で synthetic verify
- describe_skill 強制 (= H2 第 2 層 input field hallucinate)、 別 wave で実装中
- proxy 強モデル追加 (user-side) → Wave 3 G4 spike

### 設計原則の運用
- batch 7 で言語化された 4 原則 (= deterministic split / minimize speculation /
  observe before speculate / care boundary) を batch 8 で reliable に運用、
  batch 9 以降も継続

## 一言で

> **「fix 1 件 landing は chain の 1 layer 解消、 次 layer の new blocker は
> >50% 確率で露呈する」 を batch 8 が data で実証した**

— batch 7 で整備した観測 infra で 4 新 bug が確定可能になった
— prediction 設計は累積 fix verify で verified を過大評価する trap が露呈
— Reyn の chain 完走は「e2e dependency chain の全 layer green」 を要求する構造的問題

batch 8 の core narrative。
