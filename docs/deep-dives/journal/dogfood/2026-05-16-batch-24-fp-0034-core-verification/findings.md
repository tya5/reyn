# Batch 24 — Findings (FP-0034 wrapper-only e2e、 core path verification N=3)

> N=3 per scenario × 7 scenarios = **21 isolated runs**。 全 run `/tmp/reyn-b24-<sc>-r<N>/` + reyn agent `b24_<sc>_r<N>` で per-instance separation。 Wave 1 = 5 sonnet 並列 (S1A/S1B/S2/S3-noop/S3-auto)、 Wave 2 = 2 sonnet 並列 (S4 hot cold / S5 search)。

---

## 0. Run summary

| Item | Value |
|---|---|
| Branch HEAD | `4c89c20 chore(fp-0034): batch 24 prep — driver + prelude + doc syncs` |
| Mode | wrapper-only (`hide_legacy_tools=true`, `embedding_class=standard`) |
| Model | `openai/gemini-2.5-flash-lite` via LiteLLM proxy |
| Driver | `scripts/dogfood_b24_driver.py` (= 7 scenarios + per-scenario verdict 関数) |
| Total runs | 21 (= 7 scenarios × N=3) |
| Total wall-clock | ~10 min (= 2 waves 並列、 sub-agent overhead 込み) |
| LLM API cost (estimate) | ~$0.02-0.04 |

---

## 1. Verdict matrix

### 1.1 Driver verdict (= mechanism-level、 driver code 判定そのまま)

| Scenario | V | I | R | B | Verified rate |
|---|---|---|---|---|---|
| S1A (P-AND parallel-tolerant) | 1 | 2 | 0 | 0 | 33% |
| S1B (P-SEQ baseline) | 3 | 0 | 0 | 0 | 100% |
| S2 (routing_decided N=3) | 3 | 0 | 0 | 0 | 100% |
| S3-noop (gating empty) | 3 | 0 | 0 | 0 | 100% |
| S3-auto (describe path) | 1 | 2 | 0 | 0 | 33% |
| S4 (hot cold start) | 3 | 0 | 0 | 0 | 100% |
| S5 (search semantic) | 2 | 1 | 0 | 0 | 67% |
| **Total** | **16** | **5** | **0** | **0** | **76%** |

### 1.2 Analyst verdict (= 原則 12 false-attribution discipline 適用後)

| Scenario | V | I | R | B | 補正理由 |
|---|---|---|---|---|---|
| **S1A** | **3** | **0** | 0 | 0 | driver `verdict_s1a` の error-surface keyword 不完全 (= 「存在しません」 未登録)、 LLM 挙動は 3/3 ideal |
| S1B | 3 | 0 | 0 | 0 | — (consistent) |
| S2 | 3 | 0 | 0 | 0 | — |
| S3-noop | 3 | 0 | 0 | 0 | — |
| S3-auto | 1 | 2 | 0 | 0 | — (LLM が `filter` vs `category` で arg shape 揺らぎ = 真の inconclusive) |
| S4 | 3 | 0 | 0 | 0 | — (hot path 0/3 は seed 構造的制約、 cold path 3/3 = 仕様内) |
| **S5** | **0** | **0** | **0** | **3** | search_actions が tools= 不在 (= D14 `is_ready()` gate)、 structural pre-check 暗黙 fail → blocked |
| **Total** | **16** | **2** | **0** | **3** | **76%** |

両 verdict view で **verified 76%** (= analyst view が信頼性高)、 prelude 楽観予測 80% から -4pp の calibrated 着地。

---

## 2. Brier score

per-scenario per-run multi-class Brier (= sum over 4 outcomes of `(predicted_prob - actual_indicator)²`) を 3 runs で mean、 batch mean を 7 scenarios で mean。

### 2.1 Driver verdict basis

| Scenario | Predicted (V/I/R/B %) | Per-run Brier (mean) |
|---|---|---|
| S1A | 80/10/5/5 | 0.988 (= V 1run × 0.055 + I 2runs × 1.455) |
| S1B | 75/15/5/5 | 0.090 |
| S2 | 85/10/0/5 | 0.035 |
| S3-noop | 75/15/5/5 | 0.090 |
| S3-auto | 85/10/0/5 | 1.035 |
| S4 | 60/25/5/10 | 0.235 |
| S5 | 50/30/10/10 | 0.493 |
| **Batch mean** | | **0.424** |

### 2.2 Analyst verdict basis

| Scenario | Per-run Brier (mean) |
|---|---|
| S1A | 0.055 (= V 3/3 で predicted 80% close) |
| S1B | 0.090 |
| S2 | 0.035 |
| S3-noop | 0.090 |
| S3-auto | 1.035 |
| S4 | 0.235 |
| S5 | 1.160 (= B 3/3、 predicted blocked 10% から大幅乖離) |
| **Batch mean** | **0.386** |

**両 Brier ともに target 0.3-0.5 band 内** = **calibration framework 稼働確認**。

prelude 0.948 (= practice batch baseline) から **batch 23 → batch 24 で 0.42-0.39 へ -0.5+ 改善**。 batch 17-22 progression の 0.18-0.30 域に近づく。

---

## 3. Findings

### B24-S1A-1 (LOW) — Driver keyword coverage bug

**Severity**: LOW (= driver implementation issue、 LLM 挙動正常)

**Observation**: S1A 3 runs 全てで LLM が parallel dispatch + error correctly surface (= 「存在しません」 2/3、 「見つかりませんでした」 1/3)。 driver `verdict_s1a` の error keyword list は 「見つかり / not found / 失敗」 等を含むが 「存在しません」 を含まず、 結果 2/3 を inconclusive と誤判定。

**Fix candidate**: `scripts/dogfood_b24_driver.py` `_has_error_surface()` の keyword list に 「存在しません」 「ありません」 等を追加 (= 1 line edit)。

**Carry-over**: B25 driver patch wave に含める (= 30 sec fix)。

---

### B24-S3-AUTO-1 (MED) — list_actions arg shape inconsistency

**Severity**: MED (= LLM 挙動の structural ambiguity)

**Observation**: S3-auto 3 runs で list_actions args が分裂:
- Run 1: `filter="exec"` (= text substring search、 string)
- Run 2: search_actions `filter="exec"` → list_actions `filter="exec"` (= fallback)
- Run 3: `category=["exec"]` (= ideal、 array)

全 3 run で answer (= exec__sandboxed_exec) は正確、 ただし call shape の variance が高。 `filter` は valid schema param (= text substring)、 `category` は array param。 LLM が両者の使い分けを認識しきれていない。

**Hypothesis**: list_actions の tool description が `filter` vs `category` の優先関係を明示していない、 もしくは parameter description が両方を均等に提示。

**Fix candidate**: tool description で 「For category-based filtering, use `category=[...]` array. For free-text search across action names + descriptions, use `filter='...'` string.」 と明示。 batch 22 で確立した practitioner 4-part template 適用候補。

**Carry-over**: B25 fix wave (= 原則 16 multi-agent context analysis 候補)。 ただし S3-auto 自体は behavior-correct (= answer 正確) なので priority MED。

---

### B24-S4-1 (INFO) — Hot list cold start = 0/3 by design

**Severity**: INFO (= 設計上の制約、 not a bug)

**Observation**: S4 「memory に何を覚えていますか」 で hot path (= direct alias) 0/3、 cold path (= list_actions(category=['memory.entry'])) 3/3。

DEFAULT_HOT_LIST_SEED 10 entries:
- file__read, file__grep
- web__search, web__fetch
- memory.operation__remember_shared (= memory category 唯一)
- skill__skill_builder, skill__skill_improver, skill__skill_importer
- skill__mcp_search, skill__read_local_files

→ `memory.entry__*` 系は seed に **不在**、 freq=0 cold start では hot path 構造的に不可。

**Implication**: hot list は usage accumulation 後 (= multi-session use) に effective。 cold start 段階では cold path が norm。 これは設計の意図と一致 (= seed は flagship のみ、 entry は accumulate)。

**Carry-over**: dogfood-discipline doc に 「hot list cold start = 構造的に seed-only。 multi-session simulation が必要なら usage tracker pre-seed が代替」 を明記候補 (= LOW priority)。

---

### B24-S4-2 (LOW) — UX regression in empty-state narration

**Severity**: LOW

**Observation**: S4 Run 2 で LLM が空 list 結果に対して 「To tell you what I remember, I need to know the names of the memory entries. Could you please list them?」 と返答 (= empty を空と認識せず、 user に entry 名を求める tautology)。

Run 1 + 3 は honest empty acknowledgment (= 「There is no memory of past interactions」 等)。 Run 2 は **空 list を 「不明な list」 と誤 parsing** か。

**Carry-over**: tool description で list_actions の return shape (= empty array semantics) を明示候補。 S4 専用ではないので 別 finding と組み合わせて B25 fix wave 候補。

---

### B24-S5-1 (HIGH) — search_actions が tools= 不在 (= D14 gate cold-start blocking)

**Severity**: HIGH (= scenario unmeasurable + LLM hallucination 誘発)

**Observation**: S5 3 runs 全てで `search_actions` が **tools= に含まれていない**。 D14 visibility gate (`_search_visible` in `src/reyn/chat/router_tools.py`) は:
```python
_search_visible = (
    universal_wrappers_enabled
    and embedding_class is not None
    and ActionEmbeddingIndex(...).is_ready()  # ← ここで False
)
```

`is_ready()` は SQLite-WAL persistence check 経由、 cold start (= fresh `.reyn/` workspace) では `.reyn/action_index/` 不在で False を返す。 結果、 search_actions が catalog から excluded。

**LLM 反応**:
- Run 1: list_actions(filter='string') Turn 1 → 「search_actions」 Turn 2 で hallucinate (= tool name 存在しない)
- Run 2: 「search_actions」 Turn 1 hallucinate → list_actions(filter='string') Turn 2 fallback
- Run 3: list_actions(filter='string') のみ、 hallucination なし

attractor detector が 2/3 で `tool_name_hallucinate` を flag。 driver `verdict_s5_search` は `tool_names contains 'search_actions'` を verified condition としていたため Run 1 + 2 を verified と誤判定 (= driver false-positive、 これも principle 12 の 2-layer 設計の重要性を実証)。

**Root cause**: embedding index の build が **synchronous でない** 設計、 cold start でユーザーが最初のクエリを送る時点では index 不在。 prelude が `embedding_class=standard` 設定すれば search_actions が visible になると暗黙仮定していたが、 実際は **index_class 設定 + index ready の 2 条件 AND**。

**Fix candidates** (= B25 fix wave 候補):
1. **Embedding index pre-warm**: reyn web / chat 起動時に embedding build を同期 (= eager init flag)
2. **Synchronous build**: cold start の最初のクエリで index を build し終えてから LLM call
3. **Mock / disable D14 for dogfood**: dogfood mode flag で `is_ready()` を常に True
4. **Tool description annotate**: 「search_actions may be unavailable in cold-start sessions. Use list_actions(filter='keyword') as fallback」 → ただし affordance-bias 誘発の reverse 効果懸念

**Carry-over**: B24-S5 measurement は本 batch 不成立、 fix wave + 再 measure が必要。 affordance-bias measurement の prerequisite として B25 で 1+2+3 のいずれかを採用、 B26 で S5 retest。

---

### B24-S5-2 (INFO) — LLM hallucinates unavailable tool name

**Severity**: INFO (= S5-1 の downstream symptom)

**Observation**: search_actions が tools= 不在の状況で LLM が 2/3 で 「search_actions」 を call name に使用 (= hallucination)。 これは **Class C protocol-level attractor 候補** (= envelope-layer / tools= 構造から逸脱した名前 invention) と暫定分類できるが、 S5-1 fix 後の retest なしには confirmed evidence 不十分。

**Carry-over**: S5-1 fix 後、 search_actions visible 状態で再 measure。 visible でも LLM がそれを picks するか / list_filtered に固執するか (= true affordance-bias measurement)。

---

### B24-SP-INFO — Sanity baseline (= regression なし)

**Observation**: 全 21 runs で:
- SP chars: 2624-2636 (= isolated cwd で `project_context_path` 不在、 wrapper-only baseline 一致)
- SP legacy literal count: 3 (= `## Action categories` の `memory.operation` / `rag.operation` description 内 vocabulary、 routing instruction not)
- routing_decided event emit: S1A 3/3 + S2 3/3 = 6 件全て chain_id unique、 source / outcome 完全

B23-PRE-1 SP refactor (= commit `cf6dde2`) の effect が N=21 で stable confirmed。

---

## 4. Attractor base rate (= cross-scenario rate matrix)

| Scenario | Attractor count (sum over 3 runs) | Type |
|---|---|---|
| S1A | 0 | — |
| S1B | 0 | — |
| S2 | 0 | — |
| S3-noop | 0 | — |
| S3-auto | 1 | tool_name_hallucinate (= `search_actions(filter=...)` Run 2、 ただし valid wrapper、 攻撃的 semantic routing) |
| S4 | 0 | — |
| S5 | 2 | tool_name_hallucinate (= `search_actions` not in tools=、 真の hallucination) |
| **Total** | **3** | |

attractor rate = 3 / 21 = **14%** (= 主因は S5-1 の D14 gate cold-start blocking、 これを除けば 1/21 = 5%)。 wrapper-only mode の attractor base rate は低、 ただし structural pre-check gap で hallucination 誘発可能。

---

## 5. Carry-over to Batch 25 (= attractor fix wave) or Batch 26 (= N=5 stability)

### Gate decision

Per `fp-0034-progression.md`:
- attractor 0% + verified ≥ 80% → B25 skip、 B26 直行
- 中間 (65-80%) → judgment call

**現状**: verified 76% (analyst basis) / attractor 14% (= S5-1 driver)。

**Judgment**: B25 fix wave 経由が筋。 driver verdicts のうち 5 inconclusive のうち:
- 2 は driver implementation (B24-S1A-1 keyword、 30 sec fix)
- 2 は LLM behavioral (B24-S3-AUTO-1 arg shape、 tool description fix)
- 3 は structural (B24-S5-1 D14 gate、 architectural fix)

architectural fix (B24-S5-1) が B26 N=5 stability gate の prerequisite。 行わなければ S5 attractor base rate measurement 不能。

### B25 fix wave 候補 (= 原則 16 pre-fix multi-agent context analysis 適用候補)

1. **B24-S5-1 embedding index pre-warm**: reyn web/chat 起動時に sync init flag (= ~2h)、 dogfood 用に `--eager-embedding-build` CLI flag
2. **B24-S3-AUTO-1 list_actions tool description refinement**: `category` vs `filter` 優先関係明示 (= 1h、 practitioner 4-part template)
3. **B24-S1A-1 driver keyword expansion**: `_has_error_surface()` keyword list 拡張 (= 30 sec)
4. **B24-S4-2 list_actions return shape annotation**: empty array semantics 明示 (= 30 min、 B24-S3-AUTO-1 と combine)

**Estimated wall-clock**: ~3-4h (= 原則 16 pre-fix context analysis 1h + fix design + impl + test + commit)

### B26 N=5 stability prerequisites

- B24-S5-1 fix landed (= search_actions visible で affordance-bias 測定可能)
- B24-S3-AUTO-1 fix landed (= arg shape consistency 改善)
- B24-S1A-1 driver fix (= verdict accuracy 向上)

B26 で同 7 scenarios + N=5 で final verified ≥ 80% target、 production-grade phase 1 完了 milestone。

---

## 6. Methodology validation

### Worked

- **per-cwd + per-reyn-agent isolation pattern**: 21 isolated runs で session contamination 0
- **5 sonnet 並列 wave 1 + 2 並列 wave 2**: 21 runs を ~10 min wall-clock で完走、 cost-efficient
- **driver `--agent-name` flag + auto-create**: clean isolation の operational handle
- **原則 12 verdict 2-layer**: driver mechanism / analyst intent の分離が S1A / S5 で価値発揮 (= driver false-negative 1件、 false-positive 2件 catch)

### Improved this batch

- B23 で確立した isolation pattern を batch 24 で 21 runs scale 適用、 architecture stable
- B23 で確立した llm_replay --patch を本 batch では不使用 (= attractor surface 少なく synthesis 直結)、 batch 25 fix design で再活用候補

### To improve next batch

- driver verdict logic を mechanism check / intent check で構造的に分離 (= 原則 12 自動化候補、 batch 23 retrospective 提案)
- B24-S5-1 のような **structural pre-check gap** を prelude template に 「config defaults verified」 row として明示 (= batch 23 で追加済の 1 行 audit)
- batch 25 fix wave で **原則 16 pre-fix multi-agent context analysis** を S5 architectural fix に適用
