# Batch 17 (RAG-extensible OS Phase 1 — first real dogfood) — Findings

> **production grade で landed と判断していた Phase 1 が integration 経路で
> 構造的に未到達** であることを surfacing。 N=33 runs across 10 scenarios で
> **CRITICAL × 2 / HIGH × 7 / MED × 5 / LOW × 2** の bug catalog 累積。
> S2 agent が in-flight で 4 件 fix を land、 残 6 件 (CRITICAL 2 + HIGH 4) が
> Phase 1 → 1.0 release blocker。

---

## 1. Headline / TL;DR

batch 17 は ADR-0033 Phase 1 (= 12 commit、 +131 net new tests、 mkdocs strict
green、 end-to-end smoke green) を **初めて real LLM + 統合経路** に晒す batch
だった。 dogfood 前の "Phase 1 production grade" judgment は **誤り** と判明:

- **G2 (= postprocessor chain 完走)**: ChunkStrategy 出力後、 schema mismatch で
  全 run failure (B17-S2-1/2/4)。 S2 agent が in-flight 4 件 fix で landed。
- **G5 (= recall via chat)**: **0/5 invoke rate**、 R-RAG1 attractor 100%。
  Root cause が 2 重 (= B17-S6-1 build_tools 欠落 + B17-S5-3 vocab collision)。
- **G6 (= drop_source via chat)**: 0/6、 同 build_tools 欠落 + PermissionDecl
  default `index_drop=False` で permission ask 到達不可 (B17-S8-3)。
- **G4 / G7 / G8 (= empty state / memory regression / CLI)**: 構造的に動作。
  CLI 30/30 verified、 memory inline behavior 不変、 system prompt sections
  共存。 ただし empty state の LLM-level 解釈は memory layer に conflate (B17-S1-1)。

**Brier 概算**: per-scenario 平均 ~0.40 (= prediction 58% verified、 actual ~30%
verified after S2 in-flight fixes)。 batch 16 (= 0.96) より大幅改善、 batch 14
(= 0.18) には届かず、 fix wave 必須水準。

---

## 2. Per-scenario summary table

| # | Scenario | N | verified | refuted | inconclusive | blocked | Headline | New bugs |
|---|---|---|---|---|---|---|---|---|
| **S1** | Empty state UX | 3 | 0 | 3 | 0 | 0 | LLM が memory layer に conflate (= R-RAG6) | B17-S1-1 [HIGH] |
| **S2** | Index small memory | 3 | 3 | 0 | 0 | 0 | ✓ verified、 但し agent が 4 件 fix in-flight で達成 | B17-S2-1/2/3/4 [HIGH×3, MED×1] (fixed) |
| **S3** | Index Reyn docs (medium) | 3 | 3 | 0 | 0 | 0 | ✓ verified with workarounds (= S2 fix 適用後) | B17-S3-2 [MED] |
| **S4** | Index Python source | 3 | 3 | 0 | 0 | 0 | ✓ Phase 1 LLM robust、 R-RAG2 不在 | (B17-S4-1 = S2-4 重複) |
| **S5** | Recall via chat (HEADLINE) | 5 | 0 | 5 | 0 | 0 | **0% recall invoke、 R-RAG1 100%** + ctrl42 quirk | B17-S5-1 [HIGH], S5-2 [HIGH], S5-3 [MED] |
| **S6** | Multi-source recall | 5 | 0 | 0 | 0 | 5 | recall tool が build_tools に登録漏れ | **B17-S6-1 [CRITICAL]** |
| **S7** | Memory inline regression | 3 | 3 | 0 | 0 | 0 | ✓ memory inline 不変、 sections 共存 | B17-S7-1 [MED] (carry-over), S7-2 [LOW] |
| **S8** | drop_source via chat | 6 | 0 | 6 | 0 | 0 | tool 登録漏れ + PermissionDecl default で 2 重バリア | B17-S8-1 [HIGH], S8-3 [HIGH] |
| **S9** | Cost preflight gate | 3 | 0 | 3 | 0 | 0 | LLM 認識するが OS が abort candidate 不提供 | **B17-S9-1 [CRITICAL]**, S9-2 [MED] |
| **S10** | CLI smoke | 30 cases × 3 runs | 30 | 0 | 0 | 0 | ✓ CLI 全 path working | B17-S10-1 [MED], S10-2 [LOW] |

**累積**: 33 runs (= 30 + 3 from S10) executed、 bug count 16 件 (= CRITICAL × 2、 HIGH × 7、 MED × 5、 LOW × 2)。

---

## 3. Bug catalog (= severity sorted)

### CRITICAL (= release blocker、 production 完全不動作)

#### B17-S6-1 / B17-S8-2: `recall` / `drop_source` not in `build_tools()` / `_REGISTRY_DISPATCH_TOOLS`

- **症状**: ToolRegistry に登録済の `RECALL` / `DROP_SOURCE` ToolDefinition が、
  `src/reyn/chat/router_tools.py:build_tools()` および
  `src/reyn/chat/router_loop.py:_REGISTRY_DISPATCH_TOOLS` に **未追加**
- **影響**: LLM が function calling tools として認識しない → S5/S6/S8 全 blocked
- **Root cause**: Wave 2 prompt が ToolDefinition 登録のみ要求、 router 側
  wiring 統合を明示していなかった (= wrong-layer trap、 既存原則 6)
- **Fix**: `build_tools()` で `recall` / `drop_source` を tool list に追加 +
  `_REGISTRY_DISPATCH_TOOLS` frozenset に kind 名追加
- **検出 scenario**: S5 / S6 / S8 (= 同 root cause を 3 scenario で独立観測)

#### B17-S9-1: `_build_candidates()` が `abort` candidate を生成しない (= OS 設計 gap)

- **症状**: `src/reyn/kernel/runtime.py:_build_candidates()` が `finish` /
  `transition` / `rollback` の 3 種は生成するが `abort` は生成せず
- **影響**: LLM が P4 制約で `finish` を強制される → cost preflight gate (= UX
  gap fix B) を含む abort path 全体が構造的に動作しない
- **Root cause**: ADR-0033 §2.1 UX gap fix B が 「Phase 1 LLM が `decision: abort`
  を出力」 を前提に設計したが、 OS 側の candidate_outputs に `abort` が無い
  ことを未確認 (= OS layer review 漏れ)
- **Fix**: `_build_candidates()` に abort candidate 追加 (= type=abort,
  next_phase=null、 schema は ControlDecision の abort variant)
- **影響範囲**: RAG 限定でなく **全 skill の abort path** が affected (= P3/P4
  整合性問題)

### HIGH (= production user 影響)

#### B17-S1-1: Empty state UX で LLM が memory layer に conflate

- **症状**: 0 indexed sources の状態で 「what can I do? List my available data
  sources」 prompt → LLM が `## Memory` section を data sources として answer、
  `## Indexed sources (0 available)` の getting-started hint を ignore
- **影響**: HN / OSS first-touch user が 「機能ないじゃん」 first impression、
  RAG narrative 弱体化
- **Fix**: router system prompt に 「data sources」 → 「indexed sources + recall
  tool」 の disambiguation guidance 追加、 0 source 時の hint 強化

#### B17-S5-1: Gemini flash-lite が `<ctrl42>` pseudo-code を tool call 代わりに emit

- **症状**: 5 run 中 3 run で LLM が `<ctrl42>call\nprint(default_api.reyn_src_read(...))`
  形式の text reply、 actual tool_call event 不発生
- **Root cause**: model quirk (= gemini-flash-lite 特有、 production model 切替
  で部分解消の可能性)
- **Fix**: envelope-layer で pseudo-code pattern detect → reject + retry、
  もしくは prompt に明示的 「emit structured tool calls only, no pseudo-code」
- **代替**: model class を flash → strong (= claude / gpt-4) に切替で回避可能

#### B17-S5-2 / B17-S8-1: SourceManifest in-process cache が cross-process invalidation 不在

- **症状**: driver script (= 別 process) で seed した sources.yaml が、 既起動の
  `reyn web` server の per-process mem cache に反映されない
- **影響**: 動的 indexing → query loop の同 session 内 reflect が multi-process
  scenario で動作しない (= phase 1 single-process 想定の設計範囲外だが、
  driver script ベース dogfood で頻出)
- **Fix**: SourceManifest が file mtime poll を get_all() の冒頭に implementation
  (= phase 2 候補だが、 dogfood 観点で前倒し)

#### B17-S8-3: `_make_router_op_context()` で `PermissionDecl(index_drop=False)` がデフォルト

- **症状**: `src/reyn/chat/services/session.py` で `PermissionDecl` 構築時に
  `index_drop` を未渡し → デフォルト `False` → `require_index_drop()` で decl
  guard が ask UI 起動前に raise
- **影響**: 仮に B17-S6-1 fix が当たっても、 chat 経由の drop_source が permission
  ask 到達不可
- **Fix**: `_make_router_op_context()` で declared permissions に `index_drop=True`
  追加 (= ADR-0029 mcp_install パターン mirror)

### MED

- **B17-S5-3**: 「recall」 vocabulary collision (= memory intent label 「Recall」
  vs indexed search tool 「recall」)。 fix: intent label を 「Memory」 に rename +
  Behaviour disambiguation rule 追加。 S1-1 と root cause 共有。
- **B17-S9-2**: `embedding.cost_warn_threshold` config が artifact data に inject
  されず、 cost_preflight が `data.get("cost_warn_threshold")` で読めない (=
  workaround で input artifact に直書き必要)
- **B17-S10-1**: CLI rm path で `index_dropped` event が `EventStore` subscriber
  不在のため disk persistence せず、 `reyn events` replay 経路で audit trail 欠落
- **B17-S3-2**: `index_write` op が input artifact から `description` / `path`
  を受け継がず、 sources.yaml に `path: (unknown)` / `description: "Index of source X"`
- **B17-S7-1**: history bleed (= B16-S1-1 carry-over、 `clean_agent_state` が
  `history.jsonl` を wipe しない、 既存 driver issue)

### LOW

- **B17-S10-2**: CLI `--yes` flag 単独では permission gate を skip しない (=
  `permissions.index_drop: allow` or env var 必要、 by-design だが UX
  improvement 候補)
- **B17-S7-2**: LLM が memory description vagueness threshold に関係なく
  `read_memory_body` を常時呼ぶ (= 効率劣化、 cost negligible)

---

## 4. Calibration analysis

### Prediction vs actual

| Scenario | Predicted verified | Actual verified | Brier |
|---|---|---|---|
| S1 | 80% | 0% | 0.640 |
| S2 | 60% | 100% (= post-fix) | 0.040 |
| S3 | 55% | 100% (= post-fix) | 0.090 |
| S4 | 50% | 100% (= Phase 1 only) | 0.250 |
| S5 (HL) | 45% | 0% | 0.605 |
| S6 (HL) | 30% | 0% (blocked) | ~0.500 |
| S7 | 80% | 100% | 0.040 |
| S8 | 50% | 0% | 0.500 |
| S9 | 40% | 0% | 0.480 |
| S10 | 90% | 100% | 0.010 |
| **mean** | **58%** | **50%** | **~0.32** |

Brier 概算 0.32 は batch 16 (0.96) より大幅改善、 batch 14 (0.18) には届かず。
**予測がよく外れた scenario**: S1 (= R-RAG6 attractor 過小評価)、 S5/S6/S8 (=
build_tools 欠落の structural bug を予測 framework 外、 「LLM 行動」 で
predict したが root cause は wiring)、 S9 (= OS abort candidate 設計 gap も同様)。

### Pattern: structural bug が predicted attractor を mask

- **R-RAG1** (= recall invoke 忘れ) は予測通り発生したが、 **真の root cause は
  build_tools 欠落** で LLM が tool 不可視。 attractor 観測したが因果は別 layer。
- **R-RAG3** (= drop_source CLI 案内 attractor) は予測したが、 同じく build_tools
  欠落で tool 不可視 + PermissionDecl default で 2 重バリア。
- **R-RAG4** (= cost threshold ignore) は予測したが、 OS abort candidate 不在で
  LLM は ignore したかったわけではなく ignore せざるを得なかった。

→ **「LLM 行動 attractor」 と 「OS structural gap」 を切り分ける** discipline
strengthening 必要 (= 既存原則 6 「wrong layer trap」 の RAG-specific 表出)。

---

## 5. R-attractor outcomes

| ID | Description | Predicted | Observed | Notes |
|---|---|---|---|---|
| R-RAG1 | Recall tool invoke 忘れ | medium | confirmed 100% | 真の root cause は B17-S6-1 (= structural)、 vocabulary も寄与 (S5-3) |
| R-RAG2 | Phase 1 LLM が ChunkStrategy schema 違反 | low | not observed (S2/S3/S4 全 verified) | LLM が schema 遵守、 P4 機構が正しく動作 |
| R-RAG3 | drop_source invoke 忘れ | medium | confirmed 100% | 真は B17-S6-1 + S8-3 (= structural)、 attractor 観測ではない |
| R-RAG4 | Cost threshold ignore | medium | confirmed 100% | 真は B17-S9-1 (= OS gap)、 LLM は認識+abort意図ありと記録 |
| R-RAG5 | Multi-source picks 失敗 | high | unmeasurable (S6 blocked by S6-1) | post-fix retest で再評価 |
| R-RAG6 | Empty state hint hallucinate | low | confirmed 100% (S1) | memory-as-data-sources attractor、 vocabulary 起源 |

---

## 6. Headline insight: 「production grade」 judgment が誤判定だった原因

ADR-0033 の Acceptance criteria (= §6) で 12 件の boxes を ✓、 "Phase 1 完了" と
declared した。 しかし dogfood で 6 件の bug が release blocker と判明。 何が漏れたか:

### A. Acceptance criteria に "tool が LLM から見えるか" が無かった

ToolDefinition 登録 + ToolRegistry 登録は確認、 **`build_tools()` /
`_REGISTRY_DISPATCH_TOOLS` への wiring は項目化されず**。 これは ADR-0026 (=
unified tool registry) の Phase 4 で 「registry 経由 dispatch 統一」 した時に
build_tools 経路が legacy として残った wiring の二重性を、 私が認識していなかった。

### B. `abort` candidate を OS が提供する確認が無かった

UX gap fix B (= cost preflight gate) は 「Phase 1 LLM が abort 出す」 設計、 OS
側の candidate_outputs を確認せずに ADR §6 acceptance criteria を ✓ した。

### C. Tier 2 / Tier 3 test では integration を捉えられない

Wave 2I の `tests/test_index_docs_skill.py` は skill compilation を test、
Wave 2H の `tests/test_router_indexed_sources.py` は section injection を test、
Wave 2F+G の `tests/test_tool_recall.py` は ToolDefinition shape を test。
**3 件いずれも単独 layer の contract 検証、 「LLM が tool 見える」 「abort
emit できる」 の integration 経路は test 不在**。

### D. End-to-end smoke が limited

私が直前に走らせた end-to-end smoke は IndexBackend write/query/drop API のみ。
LLM 経路 (= router → tool → op) を含まなかった。 production 経路 整合性を確認
するには **chat 経由の 1 turn smoke が最低限必要**。

### 教訓

- **Acceptance criteria の 「✓」 は cross-layer integration test まで含めて初めて
  valid**。 各 wave agent が自 layer 内で test pass しても、 layer 間 wiring の
  gap は別途明示確認必要。
- **wrong-layer trap の RAG-specific variant**: ToolRegistry 登録 = "tool が
  LLM から呼べる" ではない、 build_tools への dispatch 配線が別 step。
- **structural bug は LLM-action prediction を妨害する**。 attractor 予測を
  しても、 真の root cause が OS 層なら observation が混乱する (= S5/S6/S8 で
  典型観測)。

---

## 7. Fix wave + retest plan (= 次 step)

### Fix wave A (CRITICAL): 着手 immediate

1. **B17-S6-1 / S8-2 fix**: `build_tools()` + `_REGISTRY_DISPATCH_TOOLS` への
   recall / drop_source 追加。 ~1 hour、 single-file change。
2. **B17-S9-1 fix**: `_build_candidates()` に abort candidate 追加。 ~2 hours、
   OS-layer change、 既存 skill への影響評価必要。
3. **B17-S2-1/2/3/4 fix**: ✓ S2 agent in-flight で landed (= commit 0c50a20)。

### Fix wave B (HIGH): Fix wave A の retest 後着手

4. **B17-S1-1 / S5-3 fix**: router system prompt の vocabulary disambiguation
   + 「data sources」 → 「indexed sources / recall」 mapping。 ~1-2 hours。
5. **B17-S5-2 / S8-1 fix**: SourceManifest file mtime poll on get_all()。
   ~1 hour、 phase 2 候補だった項目を前倒し。
6. **B17-S8-3 fix**: `_make_router_op_context()` で `index_drop=True` を
   PermissionDecl に declared。 ~30 min。

### Replay fixture re-record

- 4 件 (= chitchat / invoke_skill_single_round / memory_recall /
  named_skill_direct_invoke) を `REYN_LLM_RECORD=1` + LiteLLM proxy で再録音
- Fix wave 後の system prompt + tool schema 変化を反映

### Retest

- 全 fix landed 後 batch 17 retest: S1 / S5 / S6 / S8 / S9 を re-run、 N=3 each
- 目標: verified rate 70%+ (= production-grade phase 1 retake、 batch 14 mirror)
- B17-S5-1 ctrl42 は model quirk なので production model strong 切替で個別 confirm

---

## 8. Defer (= phase 1.5 以降に carry over)

| Bug | Defer 先 | 理由 |
|---|---|---|
| B17-S5-1 ctrl42 | Phase 2 model selection | model quirk、 envelope fix で部分対応可能だが strong model 切替が direct |
| B17-S7-1 history bleed | dogfood infra wave | dogfood driver issue、 production user 影響なし |
| B17-S7-2 read_memory_body always | LOW、 phase 2 prompt tuning | cost negligible |
| B17-S10-2 --yes flag | LOW、 doc clarity 拡張 | by-design behavior |

---

## 9. ADR / FP status update

ADR-0033 status は **Accepted** のままだが、 **§6 Acceptance criteria は incomplete**
と判明。 Fix wave A 完了 + retest pass 後に再判定する。 暫定 status note 追加候補。

FP-0002 status は **done** のままだが、 同様に re-evaluation 必要。

---

## 10. Conclusion

batch 17 は **「Phase 1 が production grade で landed」 という前提を覆した**
batch。 6 件 (CRITICAL 2 + HIGH 4) の release blocker bug が surfacing、 fix wave
+ retest が 1.0 release 前に必須。

batch 16 (= 0.96 Brier、 plan tool 0/25) と比べて Brier 0.32 は大幅改善、
production-grade phase 1 milestone (= batch 14 = 0.18) には届かず。 fix wave A
即着手、 wave B + retest 順次実施で batch 14 水準への復帰を目指す。

discipline 観点: 「LLM action attractor」 と 「OS structural gap」 の切り分けが
本 batch の核学習。 wrong-layer trap の RAG-specific variant、 cross-layer
integration test の必要性、 acceptance criteria の boxes が "✓" されても layer
間 wiring 確認が別途必要、 という 3 件の原則を retrospective に lift する。
