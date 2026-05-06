# Batch 14 (stability extension + meta hygiene) — Retrospective

> 🏆 **production-grade phase 1 完了 milestone** — N=5 で 5/5 (100%) complete、
> batch 13 80% → batch 14 100% で stability 完成。 全 3 fix が **🔵 不具合修正 / doc
> 追加** で 仕様変更ゼロ batch、 production user 影響なし。 batch 7-14 の 8 batch
> progression の到達点、 phase 2 (= cost / observability / production hardening)
> 移行点。

## 想定と現実のずれ

### 開始時の想定

batch 13 で real milestone 確定 (= 4/5 = 80%)、 残課題は B13-NEW-1 (= literal model
string) + B12-NEW-2/3 (= wrong-layer fixture)。 R1 fix で N=5 が 5/5 になる想定だが、
weak LLM の non-determinism で 4/5 維持 + 別 layer 露呈の可能性も見積もり。

### 実際の進行

| 想定 | 現実 |
|---|---|
| R1: B13-NEW-1 fix で 1 partial 解消 | ✅ 5/5 達成、 R1 fix が 3 fire で fallback 救済 |
| R2: fixture fix で wrong-layer trap risk 解消 | ✅ deterministic、 4 fixture rekey で完了 |
| R3: doc 化 | ✅ 2 file 追加 (permission-model + dogfood README) |
| Step 2: 4-5/5 (50% prediction) | ✅ 5/5 (predicted top zone hit) |
| 想定外シナリオ: 別 blocker 露呈 | 発生せず、 5/5 clean |

= **predictive 4/4 hit**、 batch 14 で「fix dispatch + verify cycle」 が完全に運用化。

## ターニングポイント 3 つ

### TP1: R1 fix の 3 fire 観察 (= structural fallback の operational evidence)

R1 fix (= `ModelResolver.is_known_class` + `run_skill` fallback) は test 環境で verified
だったが、 production-grade phase 1 milestone では **実 LLM dogfood で fire するか** が
critical question。

N=5 で **3 fire 観察**:
- Run 2: `gpt-3.5-turbo` literal を 2 回 intercept、 双方 fallback 成功
- Run 3: `gemini-2.5-flash-lite` literal を 1 回 intercept、 fallback 成功

= **「LLM hallucinate を OS が transparent に救済」** structural pattern の operational
evidence。 fix が test 環境のみでなく、 real LLM 挙動の中で fire し、 chain 完走に
contribute した data。

教訓: **「P3 (OS = runtime engine) + P7 (skill-agnostic) compliant fix」** が verify
される瞬間は、 N-shot で LLM hallucinate が trigger された時。 batch 14 で 3 fire 観察
したのは N=5 で "happened to encounter" だったが、 R1 fix が無ければ batch 13 の 4/5
水準に戻っていた可能性。

これは batch 7 で確立した「観測 infra」 の **OS-level fallback** 版: documented design
が LLM の不確実性を absorb する mechanism として機能。

### TP2: API 500 error → main agent 直接実行への fallback

S2 N=5 retest sonnet dispatch を 2 回試行、 両方 API 500 error で起動失敗。 通常なら
batch を pause / wait するところだが、 main agent (= Opus 1M) で直接 N=5 を sequential
実行 (= rm -rf .reyn/ → reyn chat piped → trace → repeat) で代替。

各 session ~30-60s、 total ~5 分で完了。 sonnet dispatch overhead (= worktree 作成 +
prompt 送信 + result aggregate) なしで実行可能、 mechanical sequential task は
dispatch せずとも main agent で実行が cost-efficient。

教訓: **「sonnet dispatch は dispatch overhead を考慮、 mechanical task は main agent
直接実行も candidate」**。 batch 7-13 で並列 sonnet dispatch が default だったが、
batch 14 で sequential mechanical task (= N=5 dogfood) は main agent 実行 OK と
demonstrate。 future batch の dispatch 設計時に考慮。

副次効果: API issue 時の operational resilience 強化、 single point of failure
(= sonnet dispatch) を回避する fallback path 確立。

### TP3: production-grade phase 1 完了 declaration

5/5 complete + 0 regression + Brier 0.18 で **production-grade phase 1 (= 機能成立 +
stability) 完了**。 Reyn vision (= memory `project_reyn_vision.md`) で書かれた
「Japanese enterprises with high constraints — predictability over autonomy」 の
**phase 1 機能 readiness** が data-driven に確立。

batch 7-14 の 8 batch progression:
- batch 7 (5/4): 観測 infra 整備 (= dogfood discipline 確立)
- batch 8 (5/4): 累積 fix verify (= 4 区分 prediction 導入)
- batch 9 (5/5): wrong layer trap 発見 (= verify-first principle 確立)
- batch 10 (5/5): provisional milestone (= N=1)、 reproduce-first principle 確立
- batch 11 (5/5): 80% routing-fail blocker 解消 (= G12 Pattern D)
- batch 12 (5/6): B11-NEW-1 fix (= worktree CWD vs stdlib_root() 乖離)
- batch 13 (5/6): doc 違反 fix revert + V3 wording + real milestone (= 4/5)
- **batch 14 (5/6): R1 + R2 + R3 + 5/5 = production-grade phase 1 完了**

= 各 batch が 1 layer の structural blocker を解消、 累積 8 layer 解消で milestone
到達。 「fix 1 件 = 1 layer 解消、 次 layer の new blocker が >50% 確率で露呈」
pattern (= batch 8-11 で実証) を最後まで継続、 batch 14 で **next layer なし** に到達。

## 観測 infra の継続利用 (= 8 batch 連続)

batch 7-14 で 8 batch 連続使用、 reliable: ✅
- 並列 sonnet × 4 + main agent N=5 sequential で全部活用
- `dogfood_trace --mode summary` が S2 verdict 判定の primary tool
- R1 fix の 3 fire は stderr log で確認 (= warning level、 operator visible)

道具は完成、 batch 7 投資 → 8 batch 継続回収。 batch 15+ で:
- M2 audit B12-NEW-4/5 残件 (= 軽量 hygiene)
- API 500 fallback documentation
- phase 2 設計 (= cost / observability / production hardening)

## prediction calibration の継続向上

| Batch | Brier | 主因 |
|---|---|---|
| 8 | 0.96 | 累積 fix verify の verified 過大評価 |
| 9 | 0.55 | wrong layer trap 学習 |
| 10 | 0.30 | verify-first + resolved-indirectly framework |
| 11 | 0.65 | N=1 milestone を base rate に使った overestimate |
| 12 | 0.40 | batch 11 教訓反映、 復帰 |
| 13 | 0.20 | documented design 整合性 audit、 best |
| **14** | **0.18** | **8 batch 中 best、 全 prediction zone hit** |

3/3 sub-step + Step 2 N=5 が全 hit zone 内、 Brier 微改善で 8 batch 中 best。
calibration discipline が継続向上、 phase 2 移行に向けた predictive accuracy も
高水準。

## チームダイナミクス (= user vs assistant)

batch 14 は user 介入が **2 箇所**:
- TP1 (= 「CRITICAL/HIGH はなくなった?」): 残件 honest audit を要求、 透明性確保
- TP2 (= 「ok 進めて」): batch 14 plan の承認、 自走実行に委譲

= batch 13 までの **設計レベル介入** (= 「permission system 簡潔に」 / 「iApp 参考」)
を経て、 batch 14 は **operational accountability check** (= 「残件は?」) +
**execution delegation** (= 「進めて」) の組み合わせ。 framework が成熟し、 user は
**accountability touchpoint** に focus、 execution detail は assistant に委譲。

これは batch 7 (= 設計介入) → batch 8-13 (= 残件可視化 + 戦略判断) → batch 14
(= accountability + delegation) という **user 介入の質的 progression** の最終形。
production-grade phase 1 完了に整合。

## 次 batch (= batch 15) への申し送り

### Theme 候補 (= phase 2 移行設計)

| Theme | 内容 | 優先 |
|---|---|---|
| **A: phase 2 設計 ADR** | cost / observability / monitoring の architectural review | HIGH (= phase 1 完了直後の natural transition) |
| B: G4 spike trial | `gemini-3.1-flash-lite-preview` で stability + cost 比較、 cost 10x の ROI 評価 | MED (= user 戦略判断待ち) |
| C: meta hygiene 残件 | M2 audit B12-NEW-4/5 fix + dogfood automation API fallback doc | LOW (= 軽量 follow-up) |
| D: phase 1 stability 拡張 | N=10+ 測定で 95%+ confidence 確立 | LOW (= phase 1 declaration 済) |

batch 15 は **Theme A (phase 2 設計 ADR)** が core 候補。 fix dispatch 中心から
architectural review に shift、 batch 7-14 で確立した dogfood discipline を引き続き
運用しつつ、 production-grade 観点で次の structural concern を identify する wave。

### prediction 設計

- phase 2 設計 review は **conceptual exploration**、 verified base rate を低めに設定
- code change は最小限想定、 doc / ADR landing が main deliverable
- N=10 拡張 (= Theme D) なら weak LLM ceiling を data 化、 G4 spike 判断材料

### 設計原則の運用

- 8 batch で確立した discipline (verify-first / reproduce-first / 修正分類明示 /
  documented design 整合性 audit / N≥5 stability) を継続
- **新原則候補**: 「**API issue 時の main agent fallback**」 を operational doc 化
  (= TP2 教訓)

## 一言で

> **🏆 5/5 = production-grade phase 1 完了、 batch 7-14 の 8 batch progression の
> 到達点 — R1 fix で LLM hallucinate を OS が救済 (P3+P7) する operational evidence
> 確立、 全 3 fix が 仕様変更ゼロ で documented design 維持**

— stability 完成 (= 80% → 100%)、 production user 影響なし
— Brier 0.18 で 8 batch 中 best、 calibration discipline が継続向上
— sonnet dispatch + main agent 直接実行の運用 flexibility 強化
— phase 2 (= cost / observability / production hardening) 移行点

batch 14 で「**機能成立 → stability 確保**」 の 2 段階を完成、 batch 15+ で
**「stability 確保 → production hardening」** への transition に focus する次の
chapter が開始可能。 batch 7 で言語化した「観測 infra」、 batch 9-10 の verify-first /
reproduce-first、 batch 13 の documented design 整合性 audit、 batch 14 の修正分類
明示が、 累積で **dogfood discipline framework** として確立、 next phase でも
継続運用予定。
