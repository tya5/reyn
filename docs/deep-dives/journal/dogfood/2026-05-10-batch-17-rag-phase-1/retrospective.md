# Batch 17 — Retrospective

> Phase 1 RAG dogfood、 N=33 runs、 6 release-blocker bug surface。 「production
> grade で landed」 judgment 撤回、 fix wave + retest を 1.0 release 前に実施
> 必須。 batch 7-14 で確立した 9 原則 framework に **新原則 10 (= wrong-layer
> trap の RAG variant)** + **原則 6 強化 (= cross-layer integration test 必須)**
> を追加。

---

## 1. Expected vs actual

| 項目 | 予測 (prelude §6) | 実際 |
|---|---|---|
| mean verified | 58% | ~50% (= post-fix base、 raw は ~30%) |
| Brier | ~0.40 | ~0.32 |
| HEADLINE S5 invoke rate | 45% | 0% (= structural blocked) |
| 新 bug count | 5-10 | 16 (= CRITICAL × 2 含む) |
| production-ready judgment | Phase 1 完了 | **撤回**、 fix wave 必須 |

予測の主な miss:
- **structural bug を attractor framework で予測した** (= R-RAG1/3/4 を 「LLM
  attractor」 と分類していたが、 真の root cause は OS / wiring layer)
- **acceptance criteria の completeness を過信した** (= ADR-0033 §6 の 12 boxes
  ✓ しても integration 経路は test されていなかった)

---

## 2. Turning points

### TP1: S2 agent が in-flight で 4 件 fix を land

S2 (= index small memory) の agent が dogfood 中に postprocessor schema
mismatch (= B17-S2-1/2/4) + embed provider hardcode (= S2-3) を観測 →
discipline section A5 の "fix wave" を batch 内 sub-wave で実施 → S3 / S4
の retest で fix 効果 confirm。 「dogfood agent が fix も同時に出す」 pattern
の novel 観測。 ただし root cause が systemic (= 全 indexing skill 影響) なので
fix を main repo に直接 commit したのは判断としては正しいが、 discipline 上は
"dogfood agent は read-only on src" との既存 rule に違反。 retrospective で
このルール緩和の是非を議論候補。

### TP2: S5 (HEADLINE) の 0/5 invoke rate が **structural blocked** と判明

「recall invoke 忘れ R-RAG1 attractor」 だと予測していたが、 finding 確認で
**真の root cause は build_tools() への登録漏れ** (= B17-S6-1) と判明。 S6 の
finding が同 issue を independently confirm、 S8 でも同様に確認。 S5/S6/S8 の
3 scenario が独立に同 root cause を triangulate した。 「attractor」 ではなく
「wiring gap」 と切り分け、 fix の方向性 (= prompt 強化ではなく code 修正) が
明確になった。

### TP3: S9 の cost preflight gate が OS layer 設計 gap と判明

LLM が `control.reason.summary` で 「cost threshold exceeded, aborting...」 と
**意図表明はしている** が、 `decision: "finish"` を強制された。 P4 (= LLM is
constrained decision engine) の本質: LLM は OS-provided candidates からのみ
picks。 OS が abort candidate を generate しないと、 LLM は abort 出せない。
ADR-0033 設計時に OS 側 candidate_outputs を確認しなかったのが root cause。
**OS-layer review が ADR draft phase に必須** という教訓。

---

## 3. 強化 / 新確立された原則

### 原則 6 強化: wrong-layer trap (= RAG variant)

既存の 「test fixture と runtime artifact の drift」 だけでなく、 **「ToolRegistry
登録 ≠ LLM から呼べる」** という layer 間 wiring の二重性が本 batch で surface。

具体的に:
- ToolRegistry: `register(RECALL)` で登録される → LLM が `tools=` 内に見えるとは限らない
- `build_tools()`: router-side で実際に LLM の OpenAI tool format に inject する関数
- `_REGISTRY_DISPATCH_TOOLS`: dispatch 経路で kind 名 lookup する frozenset

3 layer 同時に登録されないと LLM 不可視。 ADR-0026 (= unified tool registry)
の Phase 4 で 「registry 経由 dispatch 統一」 移行の二重性が legacy として残った
が、 私 (= main agent) も Wave 2 dispatch agent も認識せずに wrong-layer trap
に陥った。

→ **新 sub-原則 6.2**: layer 間 wiring の確認は acceptance criteria に必ず入れる。
"X が登録されている" の boxes は "X が下流の layer から見える" boxes と同時に
✓ する。

### 原則 10 (= 新): structural bug は LLM-action attractor prediction を妨害する

batch 14 までの 9 原則は LLM 行動 (= attractor 観測 + prompt fix) 中心。 batch
17 は 「LLM 行動 attractor」 と 「OS structural gap」 が同 scenario で混在 →
prediction framework が混乱。

具体的に:
- R-RAG1 (= recall invoke 忘れ) を 「LLM attractor」 と予測
- 観測: 0% invoke、 LLM が memory tool に走る → attractor confirmed と判断したくなる
- 真の root cause: B17-S6-1 (= tool が LLM 視界外)
- LLM は attractor を起こす機会も無かった (= 与えられていない tool は呼べない)

**原則 10**: dogfood で attractor を予測する前に、 **「観測対象が LLM 視界 +
OS dispatch 経路に存在するか」 の structural pre-check** を実施する。 prelude
の R-attractor table に 「pre-check status」 列追加候補。

### 原則 6.3 (= 強化): cross-layer integration test の必要性

Tier 2 (= per-layer contract) と Tier 3 (= LLM-replay) は本 batch で確認した
gap を捉えられない:

- Tier 2 `tests/test_tool_recall.py`: ToolDefinition shape を test、 ToolRegistry
  登録 confirm。 build_tools への登録は test しない。
- Tier 3 `test_replay_skill_router.py`: 既存 routing scenarios を replay。 RAG
  scenarios は新規で fixture 不在。

**新 Tier 4 (= integration smoke、 LLM replay 不要)** の必要性が surface。
- Tool registry → build_tools → router_loop dispatch の経路で 「特定 tool name
  が LLM-visible function calling tools list に含まれる」 を assert する
  integration test
- これは LLM 不要 (= deterministic、 build_tools() の出力を inspect するだけ)

ただし 「Tier 4」 を新設すると testing policy の Tier 区分の simplicity が
失われる。 既存 Tier 2 を strict 化する形でも対応可能。 testing policy revision
は別 wave で。

---

## 4. 次 batch (= batch 18 候補) への申し送り

### Carry-over findings (= Defer 表 §8 から)

- B17-S5-1 ctrl42: phase 2 model selection wave で strong model 切替時に再評価
- B17-S7-1 history bleed: dogfood infra wave (= driver script) で fix
- B17-S7-2 read_memory_body always: prompt tuning wave (= 余裕時)
- B17-S10-2 --yes flag: doc clarity 改善 (= release pre 用)

### Carry-over calibration

- attractor framework に **structural pre-check 列** 追加 (= 原則 10)
- prediction Brier 計算で 「structural bug が absorbed の場合は除外」 する
  variant 検討 (= R-RAG1 を「attractor」 として外したが、 実は wiring gap の
  混在で predict 困難)

### Batch 18 trigger

- Fix wave A (CRITICAL) + Fix wave B (HIGH) landed 後の retest を batch 18 とする。
- 目標: S1 / S5 / S6 / S8 / S9 で N=3 each、 verified rate 70%+。
- production-grade phase 1 milestone (= batch 14 mirror) 復帰判定。

---

## 5. Methodology の自己評価

### 良かった点

- **10 並列 sonnet dispatch** で wall-clock 大幅短縮 (= 1-2 hour で N=33 runs)
- **Worktree isolation** で state collision 完全防止
- **Per-scenario 独立 driver** で finding の cross-contamination なし
- **S2 agent の in-flight fix** は dogfood discipline の柔軟性を示した novel pattern
- **HEADLINE scenario S5 の N=5** で attractor base rate 測定 + structural root
  cause 識別の両方を達成

### 改善余地

- **prelude の predictions が「LLM attractor」 視点に偏っていた** (= structural
  bug の予測が弱かった)。 整 framework に structural pre-check 加える
- **dogfood-time fix の commit boundary** (= S2 agent が S5 commit に相乗り)
  が混乱を招いた。 fix commits は独立 commit で main agent が取りまとめる方が
  audit log が綺麗
- **replay fixture re-record の cost** が batch 17 中に accumulate (= 4 件
  invalid)。 dogfood 開始前に replay 状態を確認する pre-step 追加候補
- **embedding API 不在** (= OPENAI_API_KEY 不在) で FakeEmbeddingProvider 経由
  の dogfood になった。 retrieval quality verification は phase 1.5 dogfood に
  carry over、 batch 17 は infrastructure / wiring focus に絞った

---

## 6. Conclusion

batch 17 は 「Phase 1 完了」 という直前 self-judgment が **integration 経路で
未到達** だったことを surfacing した。 dogfood の本質的価値 — green test と
production reality の gap を埋める — を再確認。

6 件の release-blocker bug を fix wave で順次解消、 retest で batch 14 水準
復帰を目指す。 batch 17 の核学習 (= wrong-layer trap RAG variant、 structural
bug が attractor predict を妨害、 cross-layer integration test 必要) を retrospective
で lift し、 batch 18 prelude の R-attractor table に structural pre-check 列
追加で operationalize する。

production grade narrative の自信過剰を一度撤回、 dogfood-driven fix の sober
discipline で再構築する。
