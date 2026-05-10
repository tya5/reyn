# 2026-05-10 — Dogfood discipline evolution across batches 17-20

> 同 1 day で 4 batches (= 17/18/19/20) を回した結果、 dogfood discipline が
> **9 原則 framework から 4 階層 prediction framework + 4 dimension scenario
> design audit checklist** に進化した、 という meta-level の学びを記録。
> 個別 batch の retrospective とは別に、 **「discipline 自体がどう evolve
> したか」** + 各 batch でなぜ追加が必要だったか + future contributor が
> 同 trap を回避するための reading order を articulate する。

## 観測 — 1 day で 4 batches、 4 つの discipline 進化

### Batch 17 (= ADR-0033 RAG Phase 1 初 dogfood)

**Trigger**: 12 commits / +131 tests / mkdocs strict / e2e smoke 全 green の
状態で 「Phase 1 production grade landed」 self-declare → 直後 dogfood で 6 件
release-blocker bug 発覚。

**Lift**: **原則 10 (= structural pre-check 必須)**。 attractor 命名の前に、
expected path が LLM 視界 + dispatch 経路に存在することを decisive に確認する。
batch 17 では `recall` tool が `ToolRegistry` には登録されていたが
`build_tools()` 出力 + `_REGISTRY_DISPATCH_TOOLS` frozenset の **2 layer 別 boxes**
で漏れていた → tool が LLM 視界に届かず、 0/5 invoke。 「attractor」 と命名
しかけたが、 真因は 3-layer wiring drift (= 構造 bug)。

**Discipline 移行**: prelude template に 「structural pre-check」 row を必須化、
`verdict=blocked` (= 構造 fail) を `verdict=refuted` (= attractor) と区別する
discipline。

### Batch 18 (= 5-commit fix wave 後の retest)

**Trigger**: 4 scenarios × N=3 + S5 拡張 N=12 = 21 runs。 Headline (S5) が
0/5 → 3/3 primary verified + 拡張 83% (= dogfood log 史上最大の per-scenario
calibration recovery、 Brier 0.575 → 0.067)。 ただし secondary scenarios で
**structural fix が landing しても behavioral verified rate が 70%+ に
届かない** (= 25% primary)。

**Lift 2 件**:

1. **原則 11 (= structural × behavioral 予測軸の分離)**: scenario 予測は
   単一値ではなく `P(structural ✓) × P(behavioral ✓)` の積。 fix wave 後の
   楽観バイアス (= 「wiring 直したから 70%+ verified」) が batch 18 で
   実証された trap。
2. **原則 12 (= verdict false-attribution discipline)**: refuted / inconclusive
   / blocked の 3 区分 rules を明文化。 batch 18 S8 で fix-wave 3 件すべて
   structural verified、 ただし `reyn web` の `PermissionResolver(interactive=False)`
   で ask cycle が deny に short-circuit → 「refuted」 と分類すると 「drop_source
   attractor」 phantom evidence を作ってしまう、 「inconclusive」 で UX config
   gap (= R1 carry-over) として正しく attribute。

### Batch 19 (= self-audit revised)

**Trigger**: batch 18 carry-over の 3 件 fix wave (= B18-S9-1 cost gate strict
ordered rule + B18-S5-1 vector strip + R-RAG-srcread router prompt guidance) 後
retest。 S9 が 0/3 → 3/3 で full recovery (= cognitive-bias attractor を named
anti-attractor callout で 100% override)、 S6 は 0/3 → 0/3 で改善ゼロ。

**当初の誤り**: S6 0/3 を 「affordance-bias attractor、 prompt-layer fix
exhausted」 と即断、 「原則 13 (= attractor class taxonomy)」 として 3 class
(= cognitive-bias / affordance-bias / protocol-level) を **1 batch 1 scenario
evidence で確立** と記載。

**User 指摘 self-audit で判明した真因**: S6 の prompt 「How is recall
implemented?」 は code-reading query で、 indexed `reyn_docs` (= concept doc only)
と semantic mismatch、 さらに `reyn_src_read` description が 「Use this for
any 'how does Reyn / how does Reyn's X work?' question」 と **textbook match で
LLM の routing は正しかった**。 attractor では無く scenario design flaw。

**Lift 2 件**:

1. **Pre-retrospective discipline (= memory `feedback_pre_retrospective_discipline.md`)**:
   retrospective 執筆前に必読 3 step (= LLM trace dump / tool description /
   scenario design 前提) を operational rule 化。 observation infra が完備
   していても discipline がないと過剰一般化 trap に直行する、 という
   二次的 trap の発見。
2. **原則 13 partial validation**: Class A (= cognitive-bias、 named callout
   pattern) は S9 で valid evidence で確立、 Class B (= affordance-bias) は
   仮説 status に **downgrade**、 Class C (= protocol-level、 envelope-layer
   adapter) は既存 G12 知見の reference。

### Batch 20 (= S6 redesign with synthetic sources)

**Trigger**: batch 19 self-audit carry-over (= affordance-bias hypothesis を
valid scenario で再評価) を実行。 synthetic 「Quantum Bridge Protocol」 sources
で `reyn_src_read` affordance conflict を排除。

**Result**: verified=0/3、 refuted_b_a1=3/3 (= recall 全 run invoke だが
quantum_concepts のみ picks、 quantum_code 未 query)。 main agent が
**pre-retrospective discipline を first time 自己実行**、 LLM trace + tool
description + scenario design 前提を retrospective 執筆前に audit、 同 batch 内で
**2 度目の scenario design confound** (= prompt 「How does X work?」 自体が
concept-leaning、 structurally 1 source で十分 → rational routing と attractor
が区別不能) を self-discover。

**Lift**: **原則 14 (= scenario design audit checklist 4 dimension)**:

| Dimension | Audit point |
|---|---|
| 1. Data semantic match | indexed source content と prompt topic の semantic 一致 |
| 2. Tool affordance match | 関連 tool description と prompt の semantic conflict 不在 |
| 3. Structural source-count requirement | prompt が structurally 何 source 必要か |
| 4. Rational alternative paths | rational alternative routing path の存在 + affordance |

batch 18 が dimension 1+2 で fail、 batch 20 が dimension 3 で fail を発見、
4 つ目 (= dimension 4) は implicit だが uncodified だった。 4 dimension すべて ✓ で
初めて prelude approval、 1 row でも ⚠️ なら redesign。

## 因果分析 — なぜ 4 batches 連続で discipline が evolve したか

### 表層: ADR-0033 RAG Phase 1 が大規模 architectural addition だった

5 op kinds + ChunkMetadata schema + IndexBackend protocol + EmbeddingProvider +
SourceManifest + index_docs stdlib skill + UX gap fix 5 件 = 「framework
foundation」 として addition surface が広く、 dogfood で hit する layer が多い。
batch 17 の 6 件 release-blocker は **all different layers** (= build_tools /
candidate_outputs / vocab / mtime poll / permission decl) で、 既存 9 原則
framework では予測 / 分類 / fix layer 判定が複雑になりすぎた。

### 中層: 9 原則は behavioral attractor 中心、 RAG では structural / scenario
design が dominant

batch 7-14 (= 9 原則 framework 確立期) は LLM 行動 (= prompt fix / verify-first
/ deterministic split) が主軸。 batch 17 で **structural bug が attractor
prediction を妨害する** pattern が surface、 batch 18 で **fix wave が structural
を直しても behavioral が独立軸である** ことを実証、 batch 19-20 で **scenario
design 自体が prediction 前提を成立させる必要** がある rigor が surface。

各 batch の lift は前 batch の trap が出現してから 「これでは数値が解釈不能」
と判明したので 1 batch 1 dimension の追加で進化した、 つまり **batch ごとの
追加は局所的に rational** だった。 4 dimension audit checklist (= 原則 14) を
batch 17 の時点で持っていれば 4 batches を 1 batch に圧縮できたか、 という
反実仮想は今となっては unverifiable だが、 future contributor は **本 insight
doc 自体が 4 batches 分のリスク回避 lift** として活用可能。

### 深層: 「dogfood agent (= LLM) も dogfood の rigor を必要とする」

batch 19 で main agent (= 私、 Claude) が retrospective を書く時に LLM trace を
読まず attractor 命名 → user 指摘で軌道修正、 という流れ自体が dogfood の
本質的価値 (= 実 use と test green の gap を埋める) の **agent 自身への適用**。
batch 20 で main agent が pre-retrospective discipline を自己実行 → 同 batch 内
で confound self-discover、 これは batch 19 lesson の operational confirm。

「dogfood is for the agents that build the system as much as the system itself」 —
human-in-the-loop が本 4 batches を catch したが、 lift された discipline は
agent self-execution で同 trap を catch する infra として機能する。

## 教訓 — future contributor 向け reading order

### 4 階層 prediction framework

dogfood prediction を立てる時の階層:

1. **Structural axis** (= 原則 10、 deterministic、 binary): pre-check で「expected
   path が LLM 視界にあるか?」 を ✓ にする
2. **Behavioral axis** (= 原則 11、 stochastic、 N runs base rate): structural ✓ の
   前提で 「LLM が picks するか?」 の base rate を prior batches から推定
3. **Verdict 区分 false-attribution discipline** (= 原則 12): refuted /
   inconclusive / blocked の 3 区分 rules を厳格適用
4. **Brier calibration self-audit** (= 既存 原則 6 + N≥5 stability): prediction
   logic が prediction → verification cycle で実証されているか

### Pre-retrospective discipline (= 原則 batch 19)

retrospective 執筆前に必ず実行:

1. 当該 scenario の **LLM trace dump** (= reasoning + tool_calls + content)
2. 関連 **tool の ToolDefinition description** (= LLM 視界 signal)
3. **Scenario design 前提条件** (= prompt と data source / tool affordance の
   semantic match)

### Scenario design audit checklist (= 原則 14)

prelude 執筆時に 4 dimension すべて ✓ を確認:

1. Data semantic match
2. Tool affordance match
3. Structural source-count requirement
4. Rational alternative paths

### Behavioral attractor class taxonomy (= 原則 13、 partial validation)

- Class A (cognitive-bias) → prompt-layer named anti-attractor callout で fixable
- Class B (affordance-bias) → 仮説 status、 valid scenario での評価 pending
- Class C (protocol-level) → envelope-layer adapter (G12 既存)

「介入 layer ladder」: prompt-layer → schema-layer → envelope-layer → model-layer。

## 関連

- 9 原則 framework 確立期: `docs/deep-dives/journal/dogfood/2026-05-04..2026-05-06-batch-7..14/` 各 retrospective
- batch 17 (= 原則 10): `docs/deep-dives/journal/dogfood/2026-05-10-batch-17-rag-phase-1/retrospective.md`
- batch 17 insight (= integration gap): `docs/deep-dives/journal/insights/2026-05-10-rag-phase1-integration-gap-discovery.md`
- batch 18 (= 原則 11 + 12): `docs/deep-dives/journal/dogfood/2026-05-10-batch-18-rag-fix-retest/retrospective.md`
- batch 19 (= 原則 13 partial + pre-retrospective discipline): `docs/deep-dives/journal/dogfood/2026-05-10-batch-19-rag-attractor-fix-retest/retrospective.md`
- batch 20 (= 原則 14): `docs/deep-dives/journal/dogfood/2026-05-10-batch-20-rag-multi-source-retest/retrospective.md`
- 統合 dogfood-discipline doc: `docs/deep-dives/contributing/dogfood-discipline.md` (= 原則 11-14 + appendix case study)
- Memory: `feedback_attractor_class_taxonomy.md` / `feedback_pre_retrospective_discipline.md` /
  `feedback_scenario_design_audit_checklist.md` / `feedback_envelope_layer_fix.md`
