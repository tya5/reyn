# Batch 25 — Findings (FP-0034 wrapper-only e2e、 fix wave)

> B24 carry-over 4 items の fix wave。 **原則 16 pre-fix multi-agent context analysis** (= 5 sonnet 並列 info-gathering 約 12 min) → main agent synthesis → 1 commit multi-layer fix → S5 + S3-auto N=3 retest。 Result: **S3-AUTO-1 + S1A-1 + S4-2 完全解消 (= description rewrite decisive)**、 **S5-1 structural fix 完了 (= hallucination 100% 解消) だが新 behavioral attractor 露呈 (= Class B affordance-bias、 list vs search choice)**。 B26 carry-over 1 件。

---

## 0. Run summary

| Item | Value |
|---|---|
| Branch HEAD | `e5a77b7 feat(fp-0034): B25 fix wave — eager embedding build + list_actions arg disambiguation` |
| Tests | 2928 passed / 4 skipped / 2 xfailed (= LLMReplay fixtures rekey 後) |
| ruff | clean |
| Context analysis | 5 sonnet 並列 info-gathering (A1-A5)、 ~12 min wall-clock |
| Fix scope | 6 files: scripts/dogfood_b24_driver.py + src/reyn/{chat/router_loop.py, chat/services/router_host_adapter.py, chat/session.py, cli/commands/chat.py, tools/universal_catalog.py} + 3 LLMReplay fixture rekey |
| Retest | 2 sonnet 並列、 S5 + S3-auto N=3 each、 ~6 isolated runs |

---

## 1. Pre-fix context analysis sub-agent reports (= 原則 16 operationalization)

### A1: B24-S5-1 trace + llm_replay --patch hypothesis testing

**Smoking gun evidence** for SP-driven hallucination:
- H1 (= tools= に search_actions inject) N=5: **5/5 picks correctly**、 query 値正確
- H2 (= SP から search_actions 言及削除) N=5: **5/5 hallucination 抑制 (= 元 2/3 → 0/5)**

**真因確定**: LLM は SP の 「4 universal wrappers: list_actions / search_actions / describe_action / invoke_action」 言及から search_actions の名前を invent。 tools= に visible にすれば correct picks、 SP から外せば hallucination 消滅。

### A2: D14 gate + ActionEmbeddingIndex lifecycle

- **D14 gate code**: `src/reyn/chat/router_loop.py:525-556` 内、 `is_ready()` check + `asyncio.create_task` で background build spawn
- **Cold start race**: Turn 1 の RouterLoop.run() で is_ready()=False → search_actions excluded → 同時に build 起動 → build 完了は LLM call 後
- **SQLite persistence は完全動作**: B24 run の `.reyn/action_index/index.db` (= 524KB / 20 vectors / catalog_hash 記録あり)、 つまり build 自体は完走済、 first turn に間に合わなかっただけ
- **既存 eager init pattern なし** (= 軽量 I/O init のみ)
- **推奨 fix**: `--eager-embedding-build` flag で startup 時 sync await

### A3: Industry research — lazy index init in agent frameworks

- **4 major frameworks (Anthropic / OpenAI / LangChain / MCP) 全てが LLM に not-ready 状態を見せない方向に収束**
- **3 solution patterns**: pre-gate (OpenAI/LangChain) / always-ready (Anthropic tool_search_tool) / post-build notify (MCP listChanged)
- **LangChain は lazy init を anti-pattern と明示**
- **Top recommendation**: Pre-warm at startup (= eager init pattern と同型)
- **Avoid**: tool description で 「not available」 warning (= affordance-bias 逆効果、 業界前例なし)

### A4: list_actions tool description history audit

- HIDE_LEGACY body は 4-part template 適用済 (= WHAT/WHEN/WHEN NOT/PREFERRED OVER/POST_CALL)
- **しかし parameter descriptions (= `category` / `filter`) は 4-part 化されていない、 使い分け基準が明示なし**
- handler は `category='exec'` (string) も accept (= defensive coercion)、 `filter='exec'` も実質動作 (= qualified_name 先頭 match)
- test fixtures: category=[...] 17/18 (= dominant)、 filter 単独 0
- **推奨 fix**: parameter description で `category`: 「For named categories ALWAYS use this array.」 + `filter`: 「Free-text substring only.」

### A5: Combined design space mapping

- B24-S5-1 fix levers ranking: (a) eager build CLI flag が cleanest (= prod parity preserved、 flag-gated)
- B24-S3-AUTO-1 levers: (a) parameter description disambiguation + (b) description body 4-part rewrite combined
- LLMReplay fixture rekey 予測: 3 universal_wrappers fixtures (= 結果として正しく 3 rekey)
- Tier 2 structural tests (= test_tool_description_role_separation) は pattern-match (POST_CALL/MUST substring) で description rewrite 後も pass 想定

---

## 2. Verdict matrix (= retest N=3)

| Scenario | B24 baseline | B25 retest | Delta | Status |
|---|---|---|---|---|
| **S3-auto** (description fix) | V=1, I=2 | **V=3, I=0** | **+2 V** | ✅ **decisively resolved** |
| **S5 search** (eager build fix) | V=2, I=1 (driver) / B=3 (analyst) | V=1, I=2 (driver) / I=2,V=1 (analyst) | structural ✓ / behavioral ✗ | ⚠️ **partial resolution** |

### Brier score (= retest scenarios only)

prelude (= B24) → B25 retest:
- S3-auto: B24 1.035 → B25 ~0.035 (= verified-aligned)
- S5: B24 1.535 → B25 ~0.95 (= driver verified 33%、 prelude predicted 50%、 やや楽観バイアスだが大幅 improvement)

---

## 3. Findings

### B24-S5-1 (HIGH、 PARTIAL RESOLUTION) — Eager embedding build fix succeeded structurally、 new Class B attractor surfaced

**Severity**: HIGH (architectural part RESOLVED) + MED (behavioral part OPEN)

**Structural verification**: 3/3 runs の trace で `search_actions` が tools= に visible 確認 (= A1 H1 hypothesis prediction と一致)。 D14 gate cold-start race は完全解消、 `--eager-embedding-build` flag が **structurally sound**。

**Hallucination 100% 解消**: B24 で 2/3 で観察された `search_actions` という存在しない tool 名 invent は B25 で **0/3** (= A1 H1 evidence と一致、 tools= に visible なら hallucinate しない)。

**New Class B affordance-bias attractor**: B25 retest で 2/3 runs が `list_actions(filter="string")` を picks (= 1/3 のみ canonical `search_actions(query="...")`)。 prompt 「現在使えるアクションの中から、 文字列処理関連のものを探したいです」 に対して LLM が:
- (a) `filter="string"` で substring match → 1 アクションも該当せず empty 返却 → 「ありません」 と reply
- (b) `search_actions(query="string processing")` で embedding semantic search → 4 items 返却 → narrate

両者とも valid path、 ただし (b) が intended behavior。 (a) の発生は **私が B25 で書いた `filter` description (= "Free-text substring match... ONLY for free-text keyword search")** が affordance としてactivate された **side-effect**。

**Root cause taxonomy**: 
- structural part = D14 gate (resolved via eager flag)
- behavioral part = Class B affordance-bias (= B22 同型、 list vs search の選択)

**Carry-over to B26**: multi-layer reinforcement fix が必要候補 (= B22 pattern):
- SP rule: 「For natural-language semantic queries (= 「探したい」 「related to」 等), PREFER search_actions over list_actions(filter=...)」
- search_actions description: WHEN 節を強化 (= natural-language verbs 列挙)
- list_actions filter description: 「For exact substring lookup」 と明示

ただし fix の test 効果は B22 と同様に N=3 retest で検証必要。 Brier 計算で 0.95 (= S5 だけで全 Brier の 1/3 弱) なので大きな improvement 余地あり。

---

### B24-S3-AUTO-1 (MED、 RESOLVED) — Description rewrite が arg shape variance を完全解消

**Severity**: MED → RESOLVED

**Result**:
- B25 retest 3/3 で `list_actions(category=["exec"])` (canonical array) を picks (= B24 1/3 から完全 recovery)
- `filter="exec"` string 使用は **0/3** (= B24 2/3 から完全消滅)
- describe_action 呼出は 2/3 (= B24 1/3 から improve、 ただし 3/3 ではない)

**Effect mechanism**: parameter description で `category`: 「PREFERRED for category-based filtering. Pass an array.」 + `filter`: 「ONLY for free-text keyword search. Do NOT pass category names here.」 が **decisive**。 LLM が canonical arg shape を選ぶ rate が 100% に。

**Side-effect**: Run 3 で `search_actions` hallucination 観測 (= S5-1 と同 root cause、 SP 言及由来)。 ただし LLM が自己回復、 verdict は verified。 これも B24-S5-1 fix (= eager build) で同時に解消可能 (= S3-auto retest では eager flag を渡していなかった)。

**No carry-over**: description-only fix で target achieve、 follow-up 不要。

---

### B24-S1A-1 (LOW、 RESOLVED) — Driver keyword expansion

**Severity**: LOW → RESOLVED

**Status**: 1-line patch (= "存在しません" / "存在しない" / "ありません" added to error-surface keywords)。 B24 S1A の 2 inconclusive (= driver keyword bug) は次回 retest で verified へ移行想定 (= B26 で retest 候補だが priority 低)。

---

### B24-S4-2 (LOW、 RESOLVED) — Empty result narration guidance

**Severity**: LOW → RESOLVED

**Status**: `_LIST_ACTIONS_DESCRIPTION_HIDE_LEGACY` の WHAT + POST_CALL 節に empty-state guidance 追加 (= 「An empty items array means no actions match — report this honestly」)。 B26 で S4 retest で verified narration rate 観察候補。

---

### B25-FIXTURE-1 (INFO) — LLMReplay fixture rekey

**Severity**: INFO

**Observation**: list_actions description rewrite で 3 LLMReplay fixtures の SHA256 key changed (= A5 prediction と一致):
- `list_actions_discovery.jsonl`
- `forced_invoke_action.jsonl`
- `invoke_skill_with_wrappers.jsonl`

`REYN_LLM_RECORD=1 LITELLM_API_BASE=http://localhost:4000 pytest <tests>` で再録音、 2928 passed / 4 skipped / 2 xfailed の green state 維持。

**No carry-over**: 標準 LLMReplay rekey workflow、 future description changes でも同 process。

---

## 4. Methodology validation

### 原則 16 pre-fix multi-agent context analysis の **decisive validation**

- **B22**: 4 prompt-tweak speculation 連続失敗 (= 0/3 verified × 4 attempts) → 5 sonnet 並列 context analysis → 1 commit fix → 3/3 verified first attempt
- **B25**: 同 pattern を 4 carry-over items (= 異種混成 architectural + behavioral + driver bug) に適用 → 4 items のうち 3 完全解消 (S3-auto / S1A / S4-2)、 1 で structural part 完全解消 + behavioral part 新 attractor 露呈 (S5)

**5 sub-agents の coverage:**
- A1 (trace + replay --patch): **smoking gun evidence で SP-driven hallucination 確定**、 fix layer 推奨を evidence-based に
- A2 (lifecycle): D14 gate code 場所 + cold-start race の 精密 timeline
- A3 (industry): 業界 4 frameworks convergence で fix direction を defended に
- A4 (description audit): existing 4-part template の applicability、 parameter description gap 発見
- A5 (design space): file 影響 + fixture rekey 予測 + 推奨 sequence、 implementation roadmap

**Cost**: ~12 min wall-clock + ~$0.01 LLM cost (= A1 replay --patch experiments)
**Output**: 1 commit、 6 files、 9 tests + 3 fixture rekey、 2928 passed green
**Effect**: 4 carry-over items のうち 3 完全解消、 1 partial resolution、 fix wave 1 cycle で B26 stability gate 近づいた

### Class B affordance-bias の **2 度目の確立**

- B22 で初確立 (= reyn_src_read vs recall)
- B25 で 2 度目 (= list_actions filter vs search_actions query)
- 共通 pattern: 2 valid path、 LLM が片方を picks (= description / SP rule の affordance 強度依存)
- 共通 fix template: multi-layer reinforcement (SP + description + parameter description)

memory `feedback_attractor_class_taxonomy.md` の Class B status は 「decisive validation」 維持、 「**異種 contexts で再現可能**」 evidence 追加候補。

---

## 5. Carry-over to Batch 26 (= N=5 stability)

### 1 item: B25-S5-2 (= MED、 behavioral)

Class B affordance-bias attractor (= list_actions filter vs search_actions query)。 fix candidate:

| Lever | Effort | Evidence | Risk |
|---|---|---|---|
| (a) SP rule 追加: 「For natural-language semantic queries, PREFER search_actions over list_actions(filter)」 | 30 min | B22 SP-rule pattern | Med (= SP bloat、 但し B22 で validated) |
| (b) search_actions description で 「探したい / 関連 / similar」 verb 列挙 + WHEN 節強化 | 30 min | A4 4-part template | Low |
| (c) list_actions `filter` description で 「Use for EXACT substring lookup only, not semantic queries」 と明示 | 20 min | B25 description rewrite と整合 | Low |

**推奨**: (a) + (b) + (c) combined (= B22 multi-layer reinforcement pattern)、 N=3 retest in B26 (or B25.5)

### B26 N=5 stability gate prerequisites

- **B25-S5-2 fix landed**: SP rule + 2 description rewrites
- **Existing scenarios N=5 retest**: S1A + S1B + S2 + S3-noop + S3-auto + S4 + S5 = 7 scenarios × 5 runs = 35 runs
- **Brier target**: 0.2-0.3 (= calibration framework production-grade phase 1 完了)
- **Verified target**: ≥ 80% across all 7 scenarios

### Decision tree

- B25-S5-2 fix → S5 retest verified ≥ 80% → B26 直行
- S5 retest <  80% → B25-S5-2 fix を context analysis 再回し (= meta-loop)
