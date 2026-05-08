# Batch 11 (non-determinism reduction) — Retrospective

> 3 structural fix を sonnet 並列 dispatch で landing、 5-shot stability retest で
> **batch 10 milestone が N=1 lucky case だった事実** が判明。 R2 (G12 Pattern D) の
> 4 batch 跨ぎ戦争への決定的解消は確実な ✅、 ただし B11-NEW-1 (= B8-NEW-1 再来)
> が真の dominant blocker と確定。 「**stability 測定は N≥5 必須**」 という discipline
> 教訓を data で確立した batch。

## 想定と現実のずれ

### 開始時の想定

batch 10 で chain 完走 milestone を確認、 残った probabilistic non-determinism
(G12 25% / B9-NEW-3 50%) + B10-NEW-1 (path typo) を structural fix することで
stability 60-70% complete rate を達成、 という想定。

### 実際の進行

| 想定 | 現実 |
|---|---|
| R1: B10-NEW-1 path typo fix | ✅ landed、 ただし真因は schema strip という deeper bug (verify-first 効果) |
| R2: G12 attractor 25% rate を構造的に下げる | ✅ **0% 達成** (Pattern D fix、 N-shot 50%→0% 確認) |
| R3: B9-NEW-3 router text-reply 50% rate 改善 | ❌ 60% rate 残存 (= no significant improvement) |
| Step 2 stability 60-70% complete | ❌ **0/5 complete** (= batch 10 milestone 撤回相当の data) |

= 「**fix 単独 verify と integration stability は別次元**」 が batch 11 の核心発見。

## ターニングポイント 3 つ

### TP1: R2 G12 Pattern D fix の 4 batch 跨ぎ戦争 決定的解消

G12 attractor は batch 5 で初観測 (= B5R2-H1)、 batch 6-7 で 4 連続再発、
batch 7 で description verbosity を root cause として確定 (= Pattern A+C)、
batch 7 後半で truncation fix (Option G、 80 chars) で system prompt + list_skills
経路の Pattern A+C を 100%→0% に解消。

batch 11 R2 で **Pattern D** (= describe_skill response の routing field 1000+ chars) を
発見・解消。 N-shot replay で 5/10 → 0/10 (50%→0%)、 構造的 fix の確証。

これで G12 attractor は **3 trigger path 全て構造的に解消**:
- Pattern A (list_skills tool_response): 80 char truncation で解消 ✅ (batch 7)
- Pattern C (system prompt inline list): 80 char truncation で解消 ✅ (batch 7)
- Pattern D (describe_skill routing field): routing strip で解消 ✅ (batch 11)

教訓: **「観測 → root cause 確定 → structural fix → 別 trigger path 露呈」 を 4 batch
跨ぎで iterate**。 各 batch は「次の trigger path 1 つ」 を unblock する貢献、
累積で完全解消に到達。 これは「**fix 1 件 = 1 layer 解消**」 という batch 8-9
教訓の delayed validation。

### TP2: batch 10 milestone が N=1 lucky case だった発覚

batch 10 retrospective で「Reyn dogfood 史上初の chain 完走 via `reyn chat`」 と
書いたが、 batch 11 5-shot で 0/5。 batch 10 の Run 2 完走は B8-NEW-1
(copy_to_work permission_denied) を **偶発的 bypass** した N=1 sample だった。

N=2 sample で 1 hit を「milestone 達成」 と claim したのは calibration 不備。
batch 11 5-shot で系統的 measurement したことで、 batch 10 milestone が
**provisional (= N=1 sample)** だったことが確定。

教訓: **「N=1 で動いた」 は「機能成立」 ではない**。 chain 完走のような stability
metric は **N≥5 measurement** で確定すべき、 N=1 / N=2 は provisional milestone と
扱う。 これは batch 10 retrospective に書いた「機能成立 → stability 確保 →
production-ready」 の 3 段階のうち、 第 1 段階 (= 機能成立) すら確定していなかった
ことを示す。

これは **calibration 退行** (Brier 0.30 → 0.65) として data 化、 batch 12 prediction
設計に反映必要。

### TP3: B11-NEW-1 (= B8-NEW-1 再来) の真の dominant blocker 確定

batch 8 で発見した B8-NEW-1 は batch 9 G15 fix で resolved とされていた。
batch 10 Run 2 で chain 完走 (= G15 effective の証拠と解釈)。 ところが batch 11
5-shot で **2/2 partial sessions が同 permission_denied で停止**。

詳細観測:
- error: `Phase 'copy_to_work' preprocessor step[1] run_op (file): read from '<stdlib>/direct_llm/skill.md' was not approved`
- step[1] は `run_op` 経由の preprocessor file.read
- G15 fix は通常 `file.read` 経路を auto-approve するが、 **preprocessor `run_op`
  経由は別 code path の可能性**

= **「resolved-indirectly」 classification を batch 10 で premature にした**。
batch 10 Run 2 が偶発的 bypass しただけで、 真の resolved でなかった。

教訓: **「resolved-indirectly」 classification は N-shot verification 必須**。
batch 10 では「Run 2 で chain 完走」 を root cause fix の証拠と解釈したが、
N=1 sample では non-deterministic factor を排除できない。 N≥5 で 80%+ pass する
ことを確認してから resolved-indirectly と classify すべき。

これは batch 9-10 で確立した verify-first / reproduce-first principle の Tier 3
拡張: **resolved-indirectly classification は N-shot verification を要する**。

## 観測 infra の継続利用

batch 7-11 で 5 batch 連続使用、 reliable: ✅
- 並列 sonnet × 4 (R1 + R2 + R3 + Step 2) で全部活用
- N-shot replay (`llm_replay --n 10`) が R2 fix verify の決定的 tool
- `dogfood_trace --mode events` が B11-NEW-1 root cause 特定の primary tool
- `detect_attractor` で 0/5 attractor rate 確認

道具自体は完成、 batch 7 投資 → 5 batch 継続回収。 batch 12 では:
- B11-NEW-1 diagnose: `run_op` permission path code reading
- B11-NEW-2 diagnose: Available skills list injection verification + N=10 measurement

## prediction calibration の退行

3 batch 連続改善 (Brier 0.96 → 0.55 → 0.30) から batch 11 で退行:

| Batch | Brier score | 主因 |
|---|---|---|
| 8 | 0.96 | 累積 fix verify の verified 過大評価 |
| 9 | 0.55 | wrong layer trap 学習 |
| 10 | 0.30 | verify-first + resolved-indirectly framework 確立 |
| 11 | **0.65** | **N=1 milestone を base rate 推定に使った overestimate** |

退行は data-driven。 batch 12 calibration 教訓:
- **N=1 / N=2 sample を milestone 主張に使わない**
- **stability 測定は N≥5 で probability 確定**
- **「resolved-indirectly」 classification は N-shot verification 必須**

## チームダイナミクス (= user vs assistant)

batch 11 は user 介入が **3 箇所**、 主に rate-limit 警告系:
- TP1 (= 「もう少しでレートリミットになるかもだから気をつけて」): 並列 sonnet 投資
  の coordinated stop 準備
- TP2 (= 「再開して」): rate-limit reset 後の resume 指示
- TP3 (= 既存「subagent 並列可能なら活用して」): 並列 dispatch 承認

= batch 11 は user の **operational constraint awareness** が batch flow に直接
影響、 sonnet 並列投資の cost / time / context 利用率が user 視点で managed。
batch progression が成熟するとこの種の operational coordination が増える。

## 次 batch (= batch 12) への申し送り

### Theme: B11-NEW-1 / B11-NEW-2 fix + N≥5 stability 再測定

batch 11 で確定した dominant blocker を解消、 stability metric を batch 10 の
provisional milestone から real milestone に格上げ:

| 優先 | 内容 | scope |
|---|---|---|
| **CRITICAL** | B11-NEW-1 fix: `run_op` permission path diagnose + 適切な fix (skill_improver permissions or run_op handler 改修) | code reading + structural fix |
| HIGH | B11-NEW-2 follow-up: R3 routing fix の 60% rate 改善 (Available skills list injection verify + 例文強化) | system prompt 詳細 |
| MED | retrospective hygiene: batch 10 milestone claim を provisional に訂正 | doc update |
| MED | discipline 強化: 「resolved-indirectly」 classification は N-shot verification 必須 を testing.md or memory 化 | meta |
| MED | meta: Tier 2 fixture audit (wrong layer trap 予防) | systematic |

### prediction 設計
- N=1 / N=2 sample を base rate に使わない
- stability 測定 scenario の prediction は N≥5 measurement 想定
- structural fix の Brier base rate: verified 30-40%、 inconclusive 25-35%、
  refuted 15-25% (= batch 11 で R2 verified / R3 inconclusive / R1 partial 観察を反映)

### 設計原則の運用
- batch 7-10 で確立した 6 原則 (= 4 メタ + care boundary + verify_reproduce_first) を継続
- **新原則候補**: 「**N=1 milestone を主張しない、 stability metric は N≥5 measurement**」
  を memory 化検討

## 一言で

> **R2 G12 Pattern D fix で 4 batch 跨ぎ戦争に決定的解消、 ただし batch 10 milestone は
> N=1 lucky case だったことが 5-shot で判明 — stability 測定は N≥5 必須という discipline
> 教訓を data で確立**

— G12 Pattern D 解消 (= 50%→0%) で attractor 戦争に終止符 (batch 5-11 progression)
— batch 10 「chain 完走 milestone」 は provisional (= N=1 sample) と訂正必要
— B11-NEW-1 (= B8-NEW-1 再来、 preprocessor run_op permission path) が真の dominant blocker
— Brier 0.65 (退行) → batch 12 で N≥5 measurement で再構築

batch 11 で「**fix 単独 verify と integration stability は別次元**」 を data で確定、
「**stability 測定は N≥5 必須**」 という新 discipline 原則の motivation 確立。
batch 12 は B11-NEW-1 / B11-NEW-2 fix + N≥5 stability 再測定が theme。
