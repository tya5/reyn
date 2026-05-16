# Batch 23 — Findings (FP-0034 wrapper-only e2e practice / calibration)

> Practice batch (= 原則 7 newcomer / calibration target)。 N=1 per scenario、 fix dispatch なし、 観測 + Brier baseline + carry-over 整理が primary deliverable。

---

## 0. Run summary

| Item | Value |
|---|---|
| Branch | `feat/fp-0034-phase1-universal-catalog` |
| HEAD | `f39a2d7 chore(fp-0034): dogfood batch 23 driver` |
| Mode | wrapper-only (`hide_legacy_tools=true`, `embedding_class=standard`, `hot_list_n=10`) |
| Model | `openai/gemini-2.5-flash-lite` via LiteLLM proxy `localhost:4000` |
| Isolation | per-cwd `/tmp/reyn-b23-s<N>/` + per-reyn-agent `b23_s<N>` (= full instance separation) |
| Driver | `scripts/dogfood_b23_driver.py` (= `--agent-name` + auto-create) |
| Sub-agents | 3 sonnet 並列 (= S1 / S2 / S3、 1 LLM call ≈ 4-7s wall-clock) |

**Initial worktree-based dispatch failed** (= driver missing in worktree base, agents fell back to main repo, session contamination polluted S2)。 per-cwd + per-reyn-agent isolation で re-dispatch、 clean data 取得。

---

## 1. Verdict matrix (= 原則 12 false-attribution discipline 適用後)

| Scenario | Driver verdict | **Analyst verdict** | 主因 |
|---|---|---|---|
| S1 catalog discovery | inconclusive | **inconclusive** | 並列 dispatch (= dual-intent prompt 構造)、 describe skip |
| S2 routing_decided emit | verified | **verified** | invoke_action → P6 event emit、 permission deny path、 all structurally complete |
| S3 exec visibility | verified (routing) | **inconclusive** | LLM 経路正常だが prelude precondition mismatch (= `sandbox.backend=auto` vs 想定 `noop`)、 empty-variant gating 未 exercise |

**Driver S3 verdict 上書き根拠** (= 原則 12): S3 の prelude framing は 「noop → empty list の structural gating verification」。 actual run では gating 自体が non-noop で非発動、 LLM routing 正確性のみが verified。 scenario の primary intent は未達 → `inconclusive`。

---

## 2. Brier score (= 4-outcome 多項)

prelude 予測 (= verified / inconclusive / refuted / blocked %) と actual outcome の squared difference 総和:

| Scenario | Predicted | Actual | per-scenario Brier |
|---|---|---|---|
| S1 | V70 / I15 / R10 / B5 | I | 0.49 + 0.7225 + 0.01 + 0.0025 = **1.225** |
| S2 | V75 / I10 / R5 / B10 | V | 0.0625 + 0.01 + 0.0025 + 0.01 = **0.085** |
| S3 | V85 / I10 / R0 / B5 | I | 0.7225 + 0.81 + 0 + 0.0025 = **1.535** |
| **Batch mean** | | | **0.948** |

prelude 予測 0.2-0.4 に対して **+0.5-0.7 over-prediction** (= 楽観バイアス):
- S2 は near-perfect (= 0.085、 prelude 75% 想定 → actual verified)
- S1 / S3 で大幅 miss (= dual-intent prompt の並列 dispatch 未予測 / sandbox.backend default 仮定誤り)

practice batch の Brier ≥ 0.6 は discipline doc § 7 quickstart で expected baseline、 0.948 は **batch 8 baseline 帯** (= calibration framework 未稼働相当)。 batch 24 で 2 軸の base rate を update して 0.3-0.5 へ収束させる。

---

## 3. Findings

### B23-INFRA-1 (HIGH、 RESOLVED in-batch) — Worktree dispatch + state contamination

**Severity**: HIGH (= batch result invalidation 危険) → resolved by re-dispatch with isolated cwd

**Initial 試行 (= ab9aa9fe / a0404158 / a019793f agents)**:
- Sonnet sub-agents を `isolation: worktree` で並列 dispatch
- Worktree base が古い commit (= `18abe99` fp-0032) で driver `f39a2d7` 不在
- 2/3 agents が main repo に fallback、 `.reyn/agents/default/history.jsonl` を共有
- S2 が S1 の chat session を継続、 S2 prompt 未処理で driver が `refuted` を返す (= 真実は `blocked` = state 未隔離)

**Root cause**: 
- worktree isolation は **code 隔離** 用 (= scripts/src/) 、 **session 隔離** には不十分
- `reyn.local.yaml` は gitignored → worktree に自動 copy されない
- 各 sub-agent が main repo の `.reyn/` を共有すると history が混線

**Mitigation (= adopted in re-run)**:
- Per-cwd workspace: `/tmp/reyn-b23-s<N>/` に reyn.yaml + reyn.local.yaml を copy
- Per-reyn-agent: `reyn agent new b23_s<N>` で session 完全独立 (= dogfood-discipline §6.6 multi-shot pattern)
- Driver の `--agent-name` flag + auto-create
- 結果: 3 scenarios 全て clean isolated run 達成

**Carry-over (= batch 24+)**:
- 並列 sub-agent dispatch の standard pattern は per-cwd + per-reyn-agent (= worktree 排他または併用)
- 駆動 driver は `--agent-name` を引数として受け取る
- memory `feedback_dogfood_parallel_reyn_agent_isolation.md` で operationalize

---

### B23-S1-1 (HIGH、 behavioral — evidence-confirmed via replay --patch) — Dual-intent prompt が parallel dispatch を deterministic に誘発

**Severity**: HIGH (= prelude prediction model に systemic bias)

**Observation**: S1 prompt 「 利用可能な skill の一覧を教えて、 その中から code_review を実行してください」 で LLM が同 turn に `list_actions(category=['skill'])` + `invoke_action(action_name='skill__code_review')` を **並列 dispatch**。 describe_action は skip。 N=2 (= 1 main repo contaminated + 1 isolated) で 100% 再現。

**Hypothesis testing via `llm_replay.py --patch` (= 真因確定)**:

| H | Patch | N=5 result | 帰結 |
|---|---|---|---|
| Baseline | (none) | 100% list+invoke parallel | deterministic 確認 |
| H1 | SP 末尾に discovery-first counter-rule 追加 | 100% parallel | end-of-SP append では override 不可 |
| H2 | SP の `ROUTING RULE (ABSOLUTE)` 完全削除 | 100% parallel | **ROUTING RULE は driver ではない** |
| H3 | prompt: "code_review" 削除 (discovery only) | 100% list only | invoke は prompt 内 action 名要件 |
| H4 | prompt: discovery 削除 (execute only) | 60% invoke / 40% describe / 0% list | single intent は 2-way 確率分布 |
| H5 | "その後 / もし" sequential connector 使用 | 100% list only | sequential 言語で完全に逆転 |
| H6 | "順番に / まず" 弱 hint 追加 | 100% parallel | 弱 hint は AND-conjunction に勝てない |
| H7 | SP に ANTI-PARALLEL rule 追加 | 60% list / 40% invoke / 0% parallel | SP rule で blocking 可、 list-first 強制せず |

**真因 (= evidence-based)**: parallel dispatch は **dual-intent AND-conjunction prompt 構造** (= 「(discovery 動詞)、 その中から (execute 動詞)」) が causal driver。 `ROUTING RULE` も `code_review` 単独も driver でない。 sequential connector (= 「その後 / もし...含まれていれば」) のみが完全な seq 化、 弱 hint や末尾 SP 追加では override 不可。

**Class taxonomy 帰属**: **Class A cognitive-bias** (= LLM が evidence (= prompt の AND 構造) を 「2 並列 goal」 として weight) と暫定分類。 batch 22 affordance-bias (= Class B) と異なり、 ここでは tool affordance の混同ではなく **prompt 構造 parsing による affordance 拡張**。 single-class fit 不確定、 batch 24 で multi-scenario rate matrix で確認。

**Production-readiness implication**: 
- ✅ 並列 dispatch 自体は **bug ではない** — describe skip により 1 turn 節約、 error 時 (= 本 run のように skill 不在) は次 turn で正しく surface
- ⚠️ ただし describe_action の routing_decided audit trail / permission gate 確認の機会を失う
- ⚠️ prelude の 3-turn flow prediction model は dual-intent prompts に対して systemic 過大評価

**Carry-over (= batch 24+ candidate)**:
- (a) tool description で `Wait for previous tool result before issuing invoke_action` を invoke_action 内に明示 (= position bias 突破)
- (b) parallel-tolerant な P6 audit (= 並列 invoke でも chain_id が正しく分離されているか確認)
- (c) prelude の 4-outcome prediction model に prompt-structure 軸を追加 (= P-explicit-AND / P-explicit-SEQ / P-natural)

---

### B23-S2-1 (INFO、 positive) — routing_decided P6 event 完全動作

**Severity**: INFO (= 想定通り)

**Observation**: S2 isolated run で `invoke_action(file__read, {path: /etc/hostname})` → `routing_decided` event emit:
```json
{
  "type": "routing_decided",
  "timestamp": "2026-05-16T19:02:27.516105+09:00",
  "data": {
    "action_name": "file__read",
    "source": "invoke_action",
    "outcome": "success",
    "chain_id": "9b1dfbf309cf4053a1ade8f01381e585"
  }
}
```

`outcome=success` は **routing dispatch 成功** (= invoke_action handler 到達) を意味、 underlying file read 自体は permission gate で deny (= 正しい 2-layer 設計)。 chain_id / action_name / source 全 field intact。 Phase 3 commit `ed67850` の structural infra production-ready 確認。

**Carry-over**: なし (= verified)

---

### B23-S3-1 (MED、 scenario design) — Sandbox backend default 仮定誤り

**Severity**: MED (= prelude precondition と実装の乖離、 但し gating logic 自体は別途 verified)

**Observation**: S3 prelude が `sandbox.backend=noop` default を仮定、 「`list_actions(category=['exec'])` → empty」 を expected path と記述。 actual default は `auto` で `is_exec_available("auto")=True`、 `exec__sandboxed_exec` が enumerate された。

LLM 経路は正常 (= `list_actions` → `describe_action` → narrate)、 attractor 0、 hallucination なし。 ただし scenario の primary intent (= D14 visibility gating が `noop` 時に hide するか) は本 run で exercise されていない。

**True gating verification (= separate evidence)**: `is_exec_available("noop")=False` は code 直接呼出で確認済、 logic 自体は正しい。

**Carry-over**: 
- batch 24 で scenario を 2 variants split: (a) `sandbox.backend=noop` で empty 確認、 (b) `auto` で 1 item + describe 確認
- prelude template に 「Structural pre-check: 関連 config の actual default を `python -c '...'` で verify」 step 追加

---

### B23-SP-1 (INFO) — SP chars baseline calibration

**Severity**: INFO

**Observation**:
- Isolated workspace (`/tmp/reyn-b23-s<N>/`): SP = **2624 chars** (= wrapper-only baseline)
- Main repo workspace: SP = **3735 chars** (= +1111 chars from `project_context_path: CLAUDE.md` injection)

prelude 「~2500 chars」 予測は bare wrapper-only base、 main repo の `project_context_path` injection を未考慮。 両観測共に consistent (= 同 SP rendering、 injection の有無で差)。

**Carry-over**: prelude template に 「SP base + project_context injection size の sum で predict」 を明記。

---

### B23-SP-2 (INFO) — SP legacy literal count = 3 (= benign)

**Severity**: INFO

**Observation**: `sp_legacy_literal_count=3` が 全 isolated runs (= S1/S2/S3) で同値。 trace 内 grep で内訳:
- `memory.operation` description 内: `(remember_shared / remember_agent / forget)` = 2 literals
- `rag.operation` description 内: `(multi-source recall, drop_source)` = 1 literal

これらは **vocabulary enumeration** (= category 内 sub-operation 例示)、 **routing instruction ではない**。 P7 (= OS skill-agnostic) violation でもなく、 B23-PRE-1 SP refactor の意図的 design (= memory / RAG operation の semantic を 1 line で記述)。

**Carry-over**: `dogfood_sp_render.py --grep-legacy` の rule を「routing instruction context のみ flag、 description context は skip」 に refine 候補。

---

### B23-DOC-1 (LOW) — `detect_attractor.py` CLI doc mismatch

**Severity**: LOW

**Observation**: 私が sub-agent prompt に `python scripts/detect_attractor.py --root .reyn/` と記述、 実際の CLI は `--trace <path>` が primary flag (= `.reyn/` root を指定する flag は存在せず、 trace JSONL を直接渡す)。 S1 sub-agent が指摘。

**Carry-over**: 
- docs/reference/dogfood-attractor.md (= もし存在) の sync 確認
- sub-agent prompt template に正しい CLI 反映

---

## 4. Carry-over to Batch 24 (= core path verification N=3)

### 必須前作業 (= structural pre-check)
- [ ] **driver isolation pattern**: per-cwd + per-reyn-agent (= `b24_s<N>` naming) を standard 化
- [ ] **sandbox.backend** を batch 24 scenarios で明示 (= reyn.local.yaml override or env)
- [ ] **prompt-structure 軸** を prelude prediction model に追加: P-explicit-AND / P-explicit-SEQ / P-natural-AND / P-natural-SEQ
- [ ] **detect_attractor.py CLI** doc sync

### 新 scenario 候補
- **S1-A (= P-explicit-AND retest with parallel-tolerant verdict)**: 同 prompt、 verdict_s1 を 「parallel dispatch + correct error surface = verified」 に refine
- **S1-B (= P-explicit-SEQ baseline)**: 「 利用可能な skill を確認してください。 その後、 もし code_review があれば実行してください」 → 3-turn expected
- **S3-noop (= explicit noop override)**: reyn.local.yaml に `sandbox: {backend: noop}` 追加、 empty variant 確認
- **S3-auto (= 本 run と同等)**: default `auto` で 1 item + describe path

### 確立しなかった事項
- **Class B / C attractor の wrapper-only state での base rate** (= batch 23 N=1 scenario set では surface しなかった、 batch 24 で multi-scenario 投入)
- **hot list direct alias 呼出 rate** (= action_usage freq=0 から start、 batch 24 で usage accumulation 後測定)
- **search_actions 経路** (= embedding 設定済だが S1/S2/S3 で誘発する prompt なし、 batch 24 で natural query 投入)

---

## 5. Methodology validation

### Worked
- **llm_replay.py --patch iterative hypothesis testing**: S1 attractor 真因を 7 hypotheses × 5 calls × ~数 cent で確定 (= 推測 fix 4 iteration ~数時間 を skip)
- **Per-cwd + per-reyn-agent isolation**: 3 sub-agents 並列で session collision なし
- **REYN_LLM_TRACE_DUMP + dogfood_trace.py**: trace inspection の primary tool として functional
- **routing_decided event** (Phase 3 P6): structural audit trail として完全動作

### Improved this batch
- 初回 worktree isolation の失敗 → per-cwd pattern に pivot (= mid-batch infrastructure lift)
- 推測 fix を avoid、 evidence chain で findings draft (= principle 4 active 適用)

### To improve next batch
- prelude prediction model に prompt-structure 軸を導入
- structural pre-check に `sandbox.backend` / `project_context_path` 等の env-dependent default audit を追加
- driver verdict 関数を 「mechanism check (= driver) vs intent check (= analyst)」 で明示分離 (= 原則 12 自動化)
