# Batch 11 (non-determinism reduction) — Findings

> 3 structural fix (R1 / R2 / R3) を sonnet 並列 dispatch で landing、 5-shot stability
> retest で **0/5 complete rate** を観測。 batch 10 の「chain 完走 milestone」 は
> N=1 の non-deterministic sample だったことが判明。 R2 (G12) は ✅ 真に effective、
> R3 は inconclusive (= 60% rate 残存)、 R1 は B11-NEW-1 で masked、 という mixed result。

## Summary table

### Fix outcomes

| Fix | Commit | 真因 | Tier | e2e Verdict |
|---|---|---|---|---|
| **R1** (B10-NEW-1) | `4898ef9` | improvement_session schema が `_resolved_paths` 未宣言 → `_strip_data` silent drop → LLM が path hallucinate | structural (schema layer) | **partially confirmed** — step[0] compute_paths 成功、 ただし step[1] permission_denied で masked |
| **R3** (B9-NEW-3) | `2c14aa6` | router system prompt が「clarifications back to user」 escape を許可 → weak LLM が text-reply で stop | structural (prompt level) | **inconclusive** — 2/5 sessions で fire (= invoke_skill 直接 emit)、 3/5 sessions で text-reply 残存 (60% rate) |
| **R2** (G12 Pattern D) | `2d892e6` | `describe_skill` response が `routing` field 含み ~1000 chars → P-b verbosity threshold 超過 → empty stop attractor | structural (response level) | ✅ **verified** — 0/5 attractor、 N-shot replay でも 50%→0% 確認 |

### Step 2 5-shot stability retest

| Metric | Pre-fix (batch 10) | Post-fix (batch 11) | Delta |
|---|---|---|---|
| Complete rate | 50% (1/2、 N small) | **0%** (0/5) | **-50pp 退行** |
| Routing-fail rate | 50% (1/2) | 60% (3/5) | +10pp |
| Partial rate | 0% (0/2) | 40% (2/5) | +40pp |
| G12 attractor rate | ~25-50% | **0%** | -25-50pp ✅ |

### 検出した新 bug (= batch 12 候補)

| ID | 重要度 | 内容 |
|---|---|---|
| **B11-NEW-1** | CRITICAL | `copy_to_work` preprocessor **step[1] `run_op` (file.read)** が permission_denied、 stdlib `direct_llm/skill.md` 等への access。 同 root cause として B8-NEW-1 が tracker 化されていたが、 batch 10 Run 2 で偶発的に bypass されていただけで実際は dominant blocker。 G15 `startup_guard` auto-approve は通常 `file.read` op 経由には効くが、 preprocessor の `run_op` 経由は別 code path の可能性 |
| **B11-NEW-2** | HIGH | R3 routing fix 60% rate で **text-reply non-determinism 残存**。 system prompt rule 改変 + Available skills list 強化したが weak LLM が 3/5 sessions で skill 名 (`skill_improver`) を Available skills として認識せず clarification text-reply で stop |

## Round 別 narrative

### Round 1: 3 fix 並列 dispatch + landing

prelude landing 後、 sonnet 3 並列で R1/R2/R3 dispatch:
- R1: improvement_session schema diagnosis で **「typo でなく schema strip」 という deeper root cause** 発見 (= verify-first principle 効果)
- R2: G12 attractor の Pattern D (= describe_skill routing field) を N-shot で 50%→0% 構造的解消、 batch 7 以来の 4 batch 跨ぎ G12 戦争に決定的 fix
- R3: 60% text-reply pattern を system prompt 構造 fix で対応

3 fix sequential cherry-pick で 1016 passed (1010→1016、 +6 test、 0 regression)。

### Round 2: 5-shot stability retest で regression 発覚

Step 2 で「**0/5 complete rate**」 という想定外の結果。 期待 60-70% 完走 vs
実際 0% は **完全な prediction miss**。 詳細:

- **R2 効果は見える** (= 0/5 attractor、 0/10 N-shot replay)
- **R3 効果は inconclusive** (= 2/5 fire、 3/5 text-reply 残存、 50%→60% no significant improvement)
- **R1 効果は masked** (= step[0] OK、 ただし step[1] permission_denied で run_and_eval 未到達)
- **B11-NEW-1 (= B8-NEW-1 再来) が真の dominant blocker** であることが判明

### Round 3: batch 10 milestone の再評価

batch 10 retrospective で「Reyn dogfood 史上初の chain 完走 via `reyn chat`」 と
書いたが、 これは **N=2 sample の Run 2 のみ完走**、 かつ B8-NEW-1 (copy_to_work
permission) が偶発的に bypass されただけ。 batch 11 5-shot で 0/5 という data は
batch 10 milestone が **statistically significant でない sample** だったことを示す。

教訓: **「N=1 で動いた」 は「機能成立」 ではない**、 stability 測定 (= N≥5)
までは provisional milestone と扱うべき。 これは batch 10 retro で書いた
「機能成立 → stability 確保 → production-ready の 3 段階」 のうち、 第 1 段階
(= 機能成立) すら実は確定していなかった。

## Prediction calibration

batch 11 prelude で予測:

| Sub-step | Top prediction | Actual | Hit? |
|---|---|---|---|
| R1 (B10-NEW-1) | verified 70% | partially confirmed | near-hit |
| R2 (G12 Pattern D) | verified 35% | **verified** | hit (under-predicted) |
| R3 (B9-NEW-3) | verified 30% | inconclusive | hit |
| Step 2 (integration 5-shot) | 60-70% complete | 0% | **big miss** |

= 1/4 hit、 1/4 near-hit、 1/4 hit、 1/4 big miss。 Brier ≈ 0.65 (batch 10:
0.30 から退行)。

退行理由:
- **batch 10 single-run milestone** を base rate に使ったが、 N=1 で stability
  base rate を予測したのが overestimate
- **B8-NEW-1 を「resolved-indirectly」 と classified した batch 10 判断** が誤り
  (= batch 10 Run 2 で偶発 bypass されただけ)
- 5-shot 系統的測定では N=1 lucky case の影響が消える

新教訓 (= batch 12 への継承):
- **stability 測定は N≥5 必須**、 N=1 / N=2 は milestone 主張に使うべきでない
- **「resolved-indirectly」 classification は再 verification 必須**: 元 root cause
  fix で消えたのか、 偶発的 bypass だったのかを N-shot で確認しないと真の resolved
  でない

## A4 review (= user 感覚との差分)

- **R2 G12 Pattern D fix は 4 batch 跨ぎの戦争に決定的**: G12 は batch 5 で初観測、
  batch 6-7 で 4 連続再発、 batch 7 で root cause 確定、 batch 7 後半で truncation
  fix (Pattern A+C)、 そして batch 11 で Pattern D fix で 0% 達成。 真の structural
  解消 ✅
- **batch 10 milestone は「provisional milestone」 だった**: N=1 で chain 完走
  したが、 5-shot で 0/5。 「milestone 達成」 と書いた retrospective は **撤回
  必要**、 もしくは「provisional milestone (= N=1 確認)」 と注記
- **R3 fix の 60% rate は wording fix の限界の再実証**: batch 9 G16 でも観察した
  「weak LLM 環境で wording fix の effective 確率 < 30%」 pattern。 真の解は構造的
  もしくは G4 trigger
- **B11-NEW-1 (= B8-NEW-1 再来) は preprocessor run_op の permission path 別問題**
  の可能性: G15 fix が通常 `file.read` を auto-approve するが、 `run_op` 経由は
  別 code path? 要 batch 12 で diagnosis

## 残懸念点 + batch 12 候補

| 優先 | 内容 | 関連 |
|---|---|---|
| **CRITICAL** | B11-NEW-1 fix: copy_to_work step[1] `run_op` permission path diagnose + skill_improver permissions に stdlib glob 追加 (= deterministic fix) | Step 2 |
| HIGH | B11-NEW-2 follow-up: R3 routing fix の 60% rate を更に下げる構造的 fix (Available skills list verification + Japanese routing 例文) もしくは G4 trigger | Step 2 |
| MED | meta: batch 10 retrospective の milestone claim を「provisional (N=1)」 に訂正 | retro hygiene |
| MED | meta: 「resolved-indirectly」 classification を N-shot verification 必須に格上げ | discipline 強化 |
| LOW | G4 spike (= 強モデル併用): proxy に新規 endpoint `gemini-3.1-flash-lite-preview` 観測、 batch 12 以降で trial 候補 | user-side wait → 部分的に解禁? |

## 一言で

> **R2 G12 Pattern D は ✅ 4 batch 跨ぎ戦争に決定的解消、 R3 は inconclusive、
> R1 は masked、 そして batch 10 milestone は N=1 lucky case だったことが
> 5-shot で判明 — stability 測定が N≥5 必須という教訓**

— G12 Pattern D fix で 50%→0% 構造的解消 (= batch 5 以来 7 batch 跨ぎ history の集大成)
— batch 10 「chain 完走 milestone」 は provisional (= N=1 sample) と訂正必要
— B11-NEW-1 (preprocessor run_op permission path) が真の dominant blocker、 batch 12 で focus
— 「resolved-indirectly classification は N-shot verification 必須」 という新 discipline 教訓

batch 11 で「**fix 単独 verify と integration stability は別次元**」 と確定、
batch 12 は B11-NEW-1 + B11-NEW-2 の dominant blocker fix で再 stability 測定が theme。
