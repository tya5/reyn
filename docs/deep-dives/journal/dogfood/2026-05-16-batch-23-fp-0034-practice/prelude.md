# Batch 23 — FP-0034 universal catalog routing — practice / calibration (wrapper-only e2e)

> Practice batch (= 原則 7 newcomer batch)。 calibration が primary goal、 bug 発見は副次。
> **Wrapper-only e2e** (= `hide_legacy_tools=True`)。 ship する Phase 5 後の state を直接測る。
> 3 経路並存 transitional state は dogfood scope 外 (= code landing safety のみで product state ではない)。

---

## Carry-over context

FP-0034 Phase 1 + 2 + 3 が landing 済 (= main HEAD `ed67850`, 2899 passed)。
- Phase 1: 4 universal wrappers (list_actions / search_actions / describe_action / invoke_action) + 13/13 categories enumerable + qualified-name dispatcher
- Phase 2: ActionEmbeddingIndex (in-memory + SQLite-WAL persist) + ActionUsageTracker (JSONL + freq+recency) + hot list direct alias projection + D14 visibility gating
- Phase 3: routing_decided P6 event (= invoke_action / hot list alias 呼出後に emit)

**B23-PRE-1 解消済** (= 並行 agent `a87e7e03ee7919ca0` が commit `<TBD-after-agent-completion>` で landing)。
- 旧 SP shape: `## Plan decomposition` subsection / spawn-ack Priority block / `## Memory` / `## Indexed sources` / `## MCP` / `## Files` sections 持ち、 legacy `invoke_skill` / `list_skills` を primary routing として記述 (~9000 chars)
- 新 SP shape: 3-way intent routing (Action / Plan / Reply) in Behaviour、 tool descriptions に migrated content (~2500 chars)。 Capabilities section が 4 wrappers list に刷新、 Behaviour が 5 cross-cutting policies に整理。 SP 内に legacy tool 名の literal なし。
- Anthropic **1-tool-1-purpose 原則**準拠: 各 wrapper が単一責務、 SP は routing intent のみ記述

Phase 4-6 は **dogfood-confirmed 後** に flip:
- Phase 4 (SP §D9 category-only refactor) — **B23-PRE-1 で Phase 4 preview fix 完了済**
- Phase 5 (`hide_legacy_tools=True` default flip) — batch 26 stability 後に judge
- Phase 6 (BM25 / Anthropic tool_search_tool / shell op cleanup) — Phase 4-5 後

---

## 0. Pre-batch finding — B23-PRE-1 (= SP misalignment、**解消済**)

**Severity**: HIGH (= wrapper-only e2e の structural blocker) → **RESOLVED**
**Source**: Agent 6 trace deep-dive (= 原則 16 context analysis)
**Fix landed**: 並行 agent `a87e7e03ee7919ca0`、 commit `<TBD-after-agent-completion>`

### 観察 (= fix 前の状態、 記録として保持)

`reyn web` で生成した LLM trace dump (= SP 12,026 chars、 default config) の構造:

- `## Action categories` section: ✓ (= FP-0034 PR-3b-v で追加済)
- **`## Capabilities (routing guide)` section が legacy `list_skills` / `invoke_skill` を primary routing と記述** ← resolved
- **`## Behaviour` section の routing rule が `invoke_skill` 名前マッチングフロー詳述** ← resolved
- **`invoke_action` / `list_actions` への routing 誘導が SP に一切なし** ← resolved

### Fix (= Phase 4 preview、 新 SP shape)

`src/reyn/chat/router_system_prompt.py` (相当) の Capabilities + Behaviour section を rewrite 済:
- Capabilities: 4 universal wrappers list (list_actions / search_actions / describe_action / invoke_action)
- Behaviour: 3-way intent routing (Action intent → list_actions/invoke_action / Plan intent → plan tool / Reply intent → direct text)
- SP 内 legacy tool 名 literal: **0** (= P7 準拠)
- SP 長: ~2500 chars (= 旧 ~9000 chars から ~72% 削減)

### batch 23 への影響

- B23-PRE-1 が structural blocker だったため、 fix 前の predictions は保守的。 fix 後は:
  - LLM が `invoke_skill` を hallucinate するリスク → **消滅**
  - wrapper-only alternative path が唯一の経路 → **S1 verified rate 上昇**

---

## 1. Context analysis (= 原則 16 5-axis applied to dogfood prep)

### 1.1 Calibration history (= Agent 2)

#### Brier score 履歴 (batch 17-22)
| Batch | Brier | 主因 |
|---|---|---|
| B17 | ~0.32 | structural bug を attractor で予測した miss |
| B18 S5 | 0.067 | wiring fix 5 件で structural prereq close — log 史上最大 per-scenario recovery |
| B18 4-scenario avg | 0.723 | 楽観バイアス (structural ✓ → verified 70%+ と暗黙仮定) |
| B19 | N/A | scenario design flaw、 S9 cognitive-bias callout 100% compliance |
| B20 | 0.073 | scenario design 2nd confound |
| B21 | N/A | real e2e、 affordance-bias partial validation 初取得 |
| B22 | N/A | context-driven fix で 0/3 → 3/3 first attempt |

#### Attractor base rate (= batch 17-22 累積)
- **Class A cognitive-bias**: 1 件 観測 (B19 S9)、 Named anti-attractor callout で 100% compliance
- **Class B affordance-bias**: B21-22 で decisive validation、 multi-layer reinforcement fix で 100%
- **Class C protocol-level**: B17-22 で新規観測なし

#### Fix layer effectiveness
| Fix type | Verified rate |
|---|---|
| Prompt-tweak speculation (description rewrite 単体) | **0%** (B18-20, 4 attempts) |
| Structural code fix (wiring) | 100% structural axis |
| Named anti-attractor callout (Class A) | 100% (1 instance) |
| Multi-layer reinforcement (Class B) | 100% (B22 first attempt) |

#### FP-0034 直接 carry-over
- **SP rule > tool description が routing 真因** (B22 TP2) — **B23-PRE-1 の direct manifestation**
- **P-explicit vs P-natural の base rate gap**: 83% vs 0% (= 同一 source, batch 18 vs 21)
- **Wiring gap = attractor 区別不能** (B17 TP2): structural pre-check (= 原則 10) 必須
- **Cognitive-bias fix template**: 「Common attractor to avoid: when X, do NOT Y. Z wins over W.」

### 1.2 Wrapper-only tools= shape (= Agent 4 corrected for `hide_legacy_tools=True`)

#### tools= 実態 (= production wrapper-only state)

| Group | Count | 内訳 |
|---|---|---|
| Universal wrappers | 3-4 | list_actions / describe_action / invoke_action (+ search_actions if embedding configured) |
| Hot list direct aliases | up to 10 | DEFAULT_HOT_LIST_SEED (freq=0 から start) |
| Fixed meta | 1-2 | plan (+ ask_user phase-only なので tools= には出ない) |
| Legacy per-kind tools | **0** (`hide_legacy_tools=True`) | — |
| **計** | **~14** | (search_actions visible なら 15) |

3 経路並存 (= default の 35 tools) と比較して **約 60% reduction**。 cache efficiency も improve。

#### Hot alias 設計の懸念

10 hot aliases は description が weak (= `additionalProperties=True`, "Direct alias for X. Use invoke_action for schema details."):
- Agent 7 industry research: **description quality collapse** 業界 documented (BM25 64%, Arcade.dev)
- Agent 7: **assertive description shift 10-11x** (arXiv 2505.18135) — hot alias の description は assertive でない
- Dogfood で hot alias 呼出 rate を測定 → 低ければ description rewrite (= multi-layer reinforcement) 候補

### 1.3 Scenario design audit (= Agent 3、 4-dim audit、 wrapper-only state で reaudit)

`hide_legacy_tools=True` 前提で再評価:

| ID | クラス | Verdict (old) | Verdict (new) | Note |
|---|---|---|---|---|
| **E** | exec category visibility | ✓ ready | ✓ ready | structural verification、 legacy 不在無関係 |
| **F** | routing_decided P6 event | ✓ ready | ✓ ready | event 観察、 legacy 不在無関係 |
| **A** | Catalog discovery (3-turn) | ❌ design flaw | **✓ ready** | legacy alt 消えたので rational alt なし、 wrapper 経路が唯一 |
| **D** | Qualified name handling | ⚠️ | **✓ ready** | 同上 |
| **C** | search_actions multilingual | ⚠️ | ⚠️ embedding 設定必要 | text filter vs semantic conflict は wrapper 内のみ、 弱い |
| **B** | Hot list direct alias | ⚠️ | ⚠️ | freq=0 で seed のみ、 freq+recency 累積後に意味 (batch 24-26) |

**Practice batch 23 推薦 Top 3**: **A** (rank 1) / **F** (rank 2) / **E** (rank 3)

(= 並存解消で A が rank 1 に昇格、 wrapper の core dispatch flow を直接測れる scenario)

### 1.4 Trace deep-dive findings (= Agent 6 observation + batch 23 での想定)

#### Agent 6 観測 (= **default** config、 hide_legacy_tools=False)

agent 6 が `default` config で取った trace:
- tools=31 (= 3 wrapper + 10 hot alias + 11 legacy + 7 other)
- SP 長: 12,026 chars
- **LLM が 1 turn で tool を 1 つも呼ばずに 「 検索しています、 少々お待ちください」 と虚偽 text reply** (= 新 attractor pattern、 N=1)
  - Class C protocol-level (= post-tool empty-stop variant) 候補
  - SP misalignment (= B23-PRE-1) が原因の可能性高
- hot alias description が stub (= "Direct alias for X")
- SP 内に legacy tool 名 literal 多数 (= invoke_skill / list_skills 等)

#### batch 23 での想定 (= **wrapper-only** mode、 B23-PRE-1 fix 後)

新 SP shape + hide_legacy_tools=True で期待される trace shape:
- tools= count: ~14 (= 4 wrappers + ~10 hot aliases)。 search_actions は embedding 設定次第で +1
- SP 長: ~2500 chars (= 72% 削減)
- SP 内 legacy tool 名 literal: **0**
- Identity 短縮版、 Capabilities が 4 wrappers list のみ、 Behaviour が 3-way intent routing + 4 cross-cutting policies
- plan / ask_user は phase-only で tools= に出ない

虚偽 text reply attractor が B23-PRE-1 fix 後に再発するか: **観察対象**。 SP misalignment が真因なら fix で消滅するはず。 再発なら Class C protocol-level 独立 attractor として記録。

### 1.5 Industry research (= Agent 7、 catalog routing prior art)

#### 業界 documented patterns

| Framework | Pattern | Known attractor |
|---|---|---|
| Anthropic tool_search_tool | `defer_loading: true` + BM25/regex, 85% token reduction | BM25 64% / regex 56% accuracy、 description-quality dependent |
| Cursor (MCP) | 40-tool hard cap | tool accuracy collapse above 40 |
| Claude Code | Direct exposure | full token cost per call |
| OpenAI Agents SDK | Namespaced + tool_search deferred | 43% → 2% accuracy when 4→51 tools |
| LangChain | Tool + agent reasoning loop | multiple LLM calls per decision |

#### FP-0034 の relative position

- **業界 alignment ✓**: embedding-based search (= multilingual)、 hot list (= Anthropic 推奨 3-5 non-deferred と同型)、 2 段 layered routing
- **Anthropic 1-tool-1-purpose 原則準拠** (= B23-PRE-1 fix で達成): 各 wrapper が単一責務 (list / search / describe / invoke)、 SP は routing intent のみ記述、 tool description に機能詳細を移管
- **Reyn pioneering**: universal wrapper + hot alias + legacy の 3 経路並存 transitional state (= 業界 precedent なし) — **これは dogfood scope 外、 measurement しない**
- **wrapper-only end state**: Anthropic tool_search_tool 型に近い。 B23-PRE-1 fix により SP が tool_search_tool SP と同等の簡潔さを達成

#### 業界 documented attractors (= FP-0034 で applicable)

1. **Position bias** (δpos 0.168-0.443、 BiasBusters arXiv 2510.00307) — hot list が tools= 先頭にあると過剰選択。 hot list は prefer されるべき設計なので feature。
2. **Description quality collapse** (Arcade.dev) — hot alias の weak description 懸念
3. **Assertive description shift 10-11x** (arXiv 2505.18135) — wording で preference 1000% 変動
4. **Multilingual BM25 failure** (= 業界 consensus) — embedding 採用は正しい設計

---

## 2. Practice batch scenarios (= Batch 23、 N=1 calibration、 wrapper-only)

batch 23 目的は **wrapper-only e2e infrastructure 通過確認 + calibration**。 verified rate より 「 4-outcome 分布が predictable か」 を見る。

### S1 — Catalog discovery (= Scenario A、 wrapper core flow)

**Prompt**: `「 利用可能な skill の一覧を教えて、 その中から code_review を実行してください」`
**Expected path**: `list_actions(category=["skill"])` → `describe_action("skill__code_review")` → `invoke_action("skill__code_review", args={...})`
**Prompt class**: P-explicit (= 「 一覧を教えて」)

**Structural pre-check** (= 原則 10):
- universal_wrappers_enabled=True ✓
- hide_legacy_tools=True ✓ (= legacy not visible)
- SP Capabilities/Behaviour updated to wrapper-only ✓ **B23-PRE-1 fix 済**
- 4 wrapper が tools= に visible ✓ (= Agent 6 trace で確認済)

**4-outcome prediction** (= B23-PRE-1 fix 済前提):
- structural axis: ✓ (B23-PRE-1 fix 完了、 legacy alt 消滅で唯一経路 → wrapper)
- behavioral axis: 70% verified (= wrapper-only で alternative path 消滅、 旧 55% から +15pp)
  - 残 30%: hot alias `skill__code_review` で describe をスキップして直接 invoke (= inconclusive)、 または 3-turn 途中で満足して途切れる
- **verified: 70%** / inconclusive: 15% / refuted: 10% / blocked: 5%

### S2 — routing_decided P6 event (= Scenario F、 structural)

**Prompt**: `「 file__read を invoke_action で /etc/hostname に対して使ってください」`
**Expected path**: `invoke_action(action_name="file__read", args={"path":"/etc/hostname"})` → `routing_decided(action_name="file__read", source="invoke_action", outcome="success")` event emit

**Structural pre-check**:
- routing_decided event schema 登録済 (commit ed67850) ✓
- RouterLoop が tool_calls loop 後に emit ✓
- `_univ_enabled=True` ✓

**4-outcome prediction** (= B23-PRE-1 fix 済前提):
- structural axis: ✓ (event emit は OS 決定論、 100%)
- behavioral axis: 75% verified (= hide_legacy_tools=True + 新 SP routing 誘導で invoke_action 選択率向上)
- **verified: 75%** / inconclusive: 10% / refuted: 5% / blocked: 10% (= permission gate / file not found)

**Verify method**: `.reyn/events/<agent>/router/*.jsonl` を grep して `routing_decided` を find

### S3 — exec visibility gating (= Scenario E、 structural)

**Prompt**: `「 sandboxed コマンド実行に使える action はありますか」`
**Expected path**: `list_actions(category=["exec"])` → empty (`sandbox.backend=noop`) または `[exec__sandboxed_exec]` (real backend)

**Structural pre-check**:
- `is_exec_available(sandbox_backend)` 動作 ✓ (test 確認済)
- `_enumerate_category("exec")` D14 gate check ✓
- `RouterCallerState.sandbox_backend` plumbed ✓

**4-outcome prediction** (= B23-PRE-1 fix 済前提):
- structural axis: ✓ (100% structural)
- behavioral axis: 85% verified (= 「 sandboxed」 explicit、 ambiguity 少、 新 SP の category routing で list_actions 呼出を明示誘導)
- **verified: 85%** / inconclusive: 10% / refuted: 0% / blocked: 5%

**Variants**: (a) sandbox.backend=noop で empty、 (b) sandbox.backend=seatbelt で exec__sandboxed_exec 表示

---

## 3. Track 2 — legacy-only baseline (= optional regression sanity)

batch 23 では skip。 必要なら batch 26 spot check で:

```yaml
action_retrieval:
  universal_wrappers_enabled: false   # legacy のみ
```

scenario: 既存 e2e (= invoke_skill / read_file / web_search) が pre-FP-0034 と同等動作するか、 N=1-2 で confirm。 既存 Tier 3 fixtures (5f54515, 9aec7d9, 10081cb) でカバー済なので追加 minimal。

---

## 4. Pre-execution checklist

実行前確認:

- [ ] **B23-PRE-1 fix landed 確認** (= 並行 agent `a87e7e03ee7919ca0` の commit hash を確認、 `<TBD-after-agent-completion>`)
- [ ] 新 SP shape 確認: SP 長 ~2500 chars、 SP 内 legacy tool 名 literal = 0、 Capabilities section が 4 wrappers list
- [ ] `REYN_LLM_TRACE_DUMP=.reyn/dogfood-fp0034/batch-23.jsonl` 設定
- [ ] reyn.yaml:
  - `action_retrieval.universal_wrappers_enabled: true`
  - `action_retrieval.hide_legacy_tools: true`
  - `action_retrieval.hot_list_n: 10`
  - `embedding.classes.standard.model: openai/text-embedding-3-small`
  - `action_retrieval.embedding_class: standard`
- [ ] `reyn web --port 18081` で起動 (= main session の port と衝突回避)
- [ ] LiteLLM proxy (`localhost:4000`) 接続確認 ✓ (Agent 1)
- [ ] `.reyn/state/` / `.reyn/events/` / `.reyn/action_index/` 空 (= clean baseline)
- [ ] dogfood_trace.py / llm_replay.py / detect_attractor.py が動く ✓ (Agent 1)
- [ ] practice batch なので **fix dispatch なし**、 観測のみ。 findings は batch 24 で fix wave

---

## 5. Expected outcome summary (= wrapper-only、 B23-PRE-1 fix 済)

| Scenario | verified | inconclusive | refuted | blocked |
|---|---|---|---|---|
| S1 (catalog discovery 3-turn) | 70% | 15% | 10% | 5% |
| S2 (routing_decided emit) | 75% | 10% | 5% | 10% |
| S3 (exec visibility) | 85% | 10% | 0% | 5% |

旧予測 (= fix 前) より overall +10-15pp verified。 structural simplification (legacy alt 消滅) + wrapper-only で alternative path が唯一経路になった効果。

**Predicted batch Brier**: 0.2-0.4 (= 旧 0.3-0.5 より lower。 structural simplification + legacy alt 消滅で behavioral axis の不確実性減少。 S1 の 3-turn flow 完走率が最大 uncertainty 残)

**N**: 1 per scenario (= calibration only)

---

## 6. Post-batch deliverables

- `findings.md` — 4-outcome 分類 per scenario + structural / behavioral 軸 actual
- `findings/B23-*.md` — HIGH/MED severity finding ごと 1 file (B23-PRE-1 含む)
- `retrospective.md` — Expected vs actual / turning points / 強化 or 新原則 / batch 24 への申し送り
- B23-PRE-1 SP fix の dogfood validation (= fix が landed して batch 23 で blocked ≤ 15% に収まったか)
