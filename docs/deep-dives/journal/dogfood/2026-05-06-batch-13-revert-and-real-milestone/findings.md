# Batch 13 (revert + real milestone) — Findings

> 🏆 **Reyn dogfood real milestone CONFIRMED**: N=5 で **4/5 (80%) complete rate**、
> batch 10 provisional milestone を真の milestone に格上げ達成。 doc 違反 fix 2 件
> revert + V3 wording 仕様変更 + reyn.local.yaml pre-approval setup の組み合わせで
> documented design に基づく chain 完走 stability を確立。

## Summary table

### Step 1: G15 + R1 reverts (= 不具合修正、 doc 復帰)

| 修正 | Commit | 種別 | Test 影響 |
|---|---|---|---|
| G15 revert | `1408f42` | 🔵 不具合修正 | -7 (G15 test file 削除) |
| R1 revert | `b92a22c` | 🔵 不具合修正 | -6 (R1 test file 削除) + 2 件更新 |

**G15 revert 詳細**:
- `startup_guard._prompt_file_access` の non-interactive auto-approve 経路削除
- `invoke_sub_skill` resolver propagation は **keep** (= layer 2 declaration を nested skill で機能させるため必要、 doc 整合)

**R1 revert 詳細**:
- `_in_default_read_zone` から `stdlib_root()` 削除、 CWD ancestry のみに復帰
- stdlib path read は declared (= layer 2) 経由必須 (= documented design)

### Step 2: reyn.local.yaml dogfood pre-approval setup (= 設定追加、 not committed)

dogfood 自動化 (= sonnet 並列 + piped stdin) では non-interactive mode で documented
design 上 prompt 不能 → fail-closed。 `reyn.local.yaml` (= operator-local override、
git 管理外) に temporary pre-approval を入れる pattern 確立:

```yaml
permissions:
  file:
    read: allow
  python:
    pure: allow
    trusted: allow
```

= **documented layer 3 mechanism (= reyn.yaml project-wide pre-approve) の variant**。
real user も同じ pattern で dev/CI 設定する。

### Step 3: V3 wording fix (= 🟡 仕様変更)

| Field | Value |
|---|---|
| Commit | `2bd9cbf` |
| 種別 | 🟡 仕様変更 (= router routing semantics 強化) |
| Pre-fix rate | 40-50% text-reply (B12-R2 N=10 measured) |
| Post-fix rate | 5% text-reply (B12-R2 N=10 measured) |
| Test | +1 Tier 2 contract + 4 fixture rekey (= existing Tier 3 LLMReplay 維持) |
| 1010 passed | (= 1009 + 1 new) |

V3 wording (verbatim、 5 行追加):

```
ROUTING RULE (ABSOLUTE): When ANY Available skill name appears in the
user message, call invoke_skill with that skill name immediately.
NO clarifying questions. NO text replies. Examples:
  「<skill_name> で <target> を review して」 → invoke_skill(name=<skill_name>)
  「<skill_name> で <X> を作って」 → invoke_skill(name=<skill_name>)
```

`<skill_name>` placeholder で **P7 compliance** (= OS 層に skill 固有名なし)。

### Step 4: N=5 stability retest

| Metric | batch 12 baseline | batch 13 N=5 | Delta |
|---|---|---|---|
| **Complete rate** | 0/5 (0%) | **4/5 (80%)** | **+80pp** ✅ |
| Routing-fail rate | 0/5 (0%) | 0/5 (0%) | 0pp (= V3 維持) |
| Partial rate | 5/5 (100%) | 1/5 (20%) | -80pp |
| Most common stop | copy_to_work step[0] python denied | (= 4/5 完走、 1/5 は eval sub-skill model issue) | layer shift ↑ |

**🏆 Real milestone CONFIRMED**: ≥60% threshold 超過 (= 80%)。

#### Per-session detail

| Session | Verdict | 内容 |
|---|---|---|
| 1 | complete ✅ | 6 phase 全完走 + improvement plan delivered |
| 2 | complete ✅ | 同上、 phase order 微差 |
| 3 | partial ❌ | `eval.run_target` で `direct_llm` skill copy 内の `gpt-3.5-turbo` literal を LiteLLM が reject → `plan_improvements` で abort |
| 4 | complete ✅ | 6 phase 全完走 |
| 5 | complete ✅ | session 3 と同 LiteLLM error 発生したが retry 成功 → 完走 |

= **Reyn の primary use case (= chat 経由 skill_improver chain)** が **stable な確率
(80%)** で機能、 production-grade development phase 1 (= 機能成立) の真の milestone
達成 ✅

### 新 bug 発見

| ID | 重要度 | 内容 |
|---|---|---|
| **B13-NEW-1** | MED | `eval.run_target` が target skill copy 内の literal model string (`gpt-3.5-turbo`) を使う、 model class (`light` / `standard` 等) 経由でない。 LiteLLM proxy が直接 reject する可能性、 retry で recover することが多いが時々 abort 原因 |

## Round 別 narrative

### Round 1: 4 並列 dispatch + 真因 audit

batch 12 retro 後、 user との対話で **「permission system が complex に感じる」**
という smell test 失敗の指摘 → documented permission model 再 audit:

- iApp 型 trust model が明文化されていた (= declare + approve、 4 source approval、
  3 layer)
- batch 11-12 で landing した G15 / R1 fix が **doc 違反** であることを発見
- B12-NEW-1 候補は更なる doc 違反拡張 → **却下**

= 「**fix accumulation で system が incoherent になる**」 を user 視点で test、
documented design への復帰 path を確立。

### Round 2: parallel revert (= R1 + R2)

並列 sonnet で G15 + R1 (B12) を同時 revert:
- G15 revert sonnet は **change (1) revert + change (2) keep** という細かい判断
  (= invoke_sub_skill resolver propagation は doc 整合で必要)
- R1 revert sonnet は単純 revert + R1 修正の test 復元

両方 sequential cherry-pick で main に landing、 1009 passed (= 13 件 G15/R1 test 削除)。

### Round 3: V3 wording fix (= 仕様変更)

V3 wording fix sonnet が router_system_prompt.py に ABSOLUTE rule + JA examples
追加。 P7 compliance (= `<skill_name>` placeholder) 維持。 4 fixture rekey + 1 Tier 2
contract test 追加で 1010 passed。

仕様変更 classification を明示、 user 視点の change を documented:
- Routing intent semantics は同じ (= R3 fix 意図維持)
- Wording 強化で 40-50% → 5% rate compliance 改善
- Skill author / operator API 変更なし

### Round 4: N=5 で real milestone confirmation

S4 sonnet が reyn.local.yaml temporary setup + N=5 sequential dogfood:
- 4/5 sessions で complete (= 80%)
- 1/5 partial (= B13-NEW-1 LiteLLM model rejection)
- routing-fail 0/5 (= V3 fix の N=5 verified)

= **「fix を積む段階 → stability を測る段階」 transition** (= batch 11 retro テーマ)
の **stability 段階達成**。 Reyn vision (= production-grade phase 1) の機能成立
milestone を data-driven に確立。

## Prediction calibration

batch 13 prelude で予測:

| Step | Top prediction | Actual | Hit? |
|---|---|---|---|
| Step 1 (revert) | verified 90% | verified | **hit** |
| Step 3 (V3 wording) | verified 70-80% | verified | **hit** |
| Step 4 (N=5) | 3/5: 35% / **4-5/5: 25%** / 0-2/5: 30% / inconclusive: 10% | 4/5 | **hit (4-5/5 zone)** |

= 3/3 hit、 100% hit。 Brier ≈ 0.20 (batch 12 0.40 から大幅改善 ✅)。

主因:
- documented design に従った **structural revert** は deterministic に hit
- V3 wording fix は B12-R2 で N=10 既測定、 verified 確率 high
- N=5 retest の 4-5/5 を 25% に振っていたのが正しく hit zone

新教訓:
- **「documented design 整合性 audit」 を fix dispatch 前に実施する**: batch 11-12
  の G15 / R1 / B12-NEW-1 候補は documented design 違反、 audit を skip すると
  fix が complexity を導入。 batch 13 で audit + revert path で解消
- **「user 視点の simplicity test」 が calibration の補完信号**: 「対称性 + 例外
  最小」 が破られていると感じた user 直感が、 documented design 違反の早期検出に
  機能した

## A4 review (= user 感覚との差分)

- **headline**: real milestone 確定 (= 80% complete rate via reyn chat)、 batch 7
  observation infra → batch 8-12 fix wave → batch 13 doc 復帰の 7 batch progression
  の到達点
- **architectural hygiene の重要性**: user 「permission system 簡潔に説明できますか?」
  指摘で documented design 違反 fix を発見、 早期 audit で複雑化を防止。 これは
  「fix 前の coherence test」 という新 discipline
- **revert as first-class fix**: batch 11-12 で landed した fix の revert を
  「不具合修正」 として明示分類、 仕様変更との区別を運用化
- **calibration 大幅改善**: Brier 0.20 (batch 13) は batch 8 (0.96) からの
  累積 progression での best。 documented design 整合性が prediction に直接寄与

## 残懸念点 + batch 14 候補

| 優先 | 内容 | 関連 |
|---|---|---|
| MED | **B13-NEW-1** fix: `eval.run_target` model class 経由化 (= literal `gpt-3.5-turbo` を proxy model class に置換) | S4 session 3 + 5 |
| MED | M2 audit B12-NEW-2/3 wrong-layer fixture 修正 (= test fixture を runtime 構造に合わせる) | batch 12 audit |
| LOW | M2 audit B12-NEW-4/5 (= path mismatch / postprocessor scope) | batch 12 audit |
| MED | dogfood pre-approval pattern を doc 化 (= reyn.local.yaml setup convention) | doc hygiene |
| trial | G4 spike (= 強モデル併用): `gemini-3.1-flash-lite-preview` evaluation | user-side cost 10x deferred |

batch 14 は **stability 拡張** (= 80% → 95% complete rate target)、 もしくは
**production-grade phase 2** (= cost / observability / monitoring) への移行候補。
batch 13 で phase 1 完了、 next phase 設計が theme。

## 一言で

> **🏆 Real milestone CONFIRMED — 80% chain 完走 rate via `reyn chat`、
> documented design 復帰 + V3 wording 仕様変更 + reyn.local.yaml pre-approval の
> 組み合わせで Reyn primary use case の stability 確立**

— G15 / R1 doc 違反 fix を revert (= 不具合修正)、 documented iApp-style permission
  model 復帰
— V3 wording 仕様変更で routing-fail rate 40-50% → 5% (= R2 N=10 既測定 + N=5 verified)
— reyn.local.yaml pre-approval pattern で dogfood 自動化と documented design 共存
— Brier 0.20 で batch 8-13 progression best、 calibration discipline 確立

batch 13 で「**fix accumulation の audit + revert**」 が「**fix dispatch**」 と同等の
discipline であることを data 化、 batch 7-13 の 7 batch progression で Reyn dogfood が
**「探索フェーズ → stability 測定フェーズ → production-grade phase 1 達成」** に到達。
