# Batch 23 — Retrospective

> FP-0034 wrapper-only e2e 初 dogfood (= practice / calibration)。 N=1 × 3 scenarios、 fix dispatch なし。 **mid-batch で infrastructure pivot** (= worktree isolation 失敗 → per-cwd + per-reyn-agent isolation) と **llm_replay.py --patch で S1 attractor 真因 evidence-isolation** が headline。 Brier 0.948 は practice batch baseline 帯、 batch 24 で 0.3-0.5 へ収束 target。

---

## 1. Expected vs actual

| Scenario | Prediction (= V / I / R / B %) | Actual | Hit/Miss |
|---|---|---|---|
| S1 catalog discovery | V70 / I15 / R10 / B5 | I (inconclusive) | **miss** — 並列 dispatch attractor (= dual-intent prompt) 未予測 |
| S2 routing_decided emit | V75 / I10 / R5 / B10 | V (verified) | hit (= near-perfect, Brier 0.085) |
| S3 exec visibility | V85 / I10 / R0 / B5 | I (inconclusive、 原則 12 verdict 補正) | **miss** — sandbox.backend default 仮定誤り |

**Batch Brier**: 0.948 (= prelude 予測 0.2-0.4 から +0.5-0.7 楽観バイアス)

### 楽観バイアスの 2 source
1. **dual-intent prompt** → 3-turn flow と暗黙的仮定 (= actual: 並列 dispatch deterministic)
2. **sandbox.backend default = noop** と仮定 (= actual: `auto`、 gating 非発動)

両方とも **structural pre-check の gap**: prelude を書いた時点で 「LLM 経路を実 trace で確認」 「実 config を `load_config()` で確認」 を完全には実施せず、 prediction model に bake された assumption が観測で fail。

---

## 2. Turning points

### TP1: Worktree isolation failure → per-cwd pivot (mid-batch infrastructure lift)

初回 sub-agent dispatch (= `isolation: worktree`) で:
- Worktree base が古い commit (`18abe99` fp-0032) で driver `f39a2d7` 不在
- 2/3 agents が main repo に fallback、 `.reyn/agents/default/history.jsonl` 共有
- S2 が S1 の chat session を継続、 `refuted` 誤判定

user 指摘 「reyn agent を claude subagent ごとに新しく作ったら？」 + 「完全独立させるなら reyn 実行インスタンス自体分けるべき」 で pivot:
- per-cwd workspace (`/tmp/reyn-b23-s<N>/`) に reyn.yaml + reyn.local.yaml copy
- per-reyn-agent (`b23_s<N>`) で session 完全独立
- driver に `--agent-name` flag + auto-create

re-dispatch で 3 scenarios 全て clean isolated run、 contamination ゼロ。

**Lesson**: worktree isolation は code-only、 session 隔離には不十分。 dogfood-discipline §6.6 multi-shot pattern を直接思い出していれば 1 cycle skip 可能だった。

### TP2: llm_replay.py --patch で S1 真因 evidence-isolation

user 指摘 「context 分析をして、 litellm に直接 context 投げて、 context 修正して、 を繰り返してどうなるか見るというデバッグ方法」 を operationalize。 7 hypotheses × N=5 × ~35 LLM calls で:

- H2 で `ROUTING RULE (ABSOLUTE)` 削除 → 100% parallel persists → **ROUTING RULE は driver でない**
- H5 で sequential connector 言語 → 100% list only → **prompt 構造が causal**
- H3 で action 名削除 → invoke 消失 → **invoke は action 名要件 (= ただし dual-intent 不在では list のみ)**

**Class A cognitive-bias 暫定分類**: LLM (= gemini-2.5-flash-lite) は 「(動詞), その中から (動詞)」 構造を 2 並列 goal と parse、 dual-intent prompts で deterministic に並列 dispatch。

これがなければ findings.md で 「ROUTING RULE が攻撃的すぎる」 等の **misattributed fix candidate** を出していた可能性。 batch 22 で確立した 「prompt-tweak speculation 4 連続 fail」 trap を避けた。

**Lesson**: behavioral anomaly 観察 → 即 `llm_replay --patch` loop が cost-effective (= ~数 cent vs 数時間 speculation iteration)。 user 「素晴らしい」 validation = standard pattern として今後採用。

### TP3: 原則 12 verdict 補正 — driver verdict vs analyst verdict 分離

S3 で driver が `verified` を返したが、 prelude framing (= noop empty variant 確認) は未 exercise。 driver は mechanism check (= `list_actions(category=['exec'])` was called)、 analyst は intent check (= structural gating exercised?) で 2 layer 分離。

原則 12 (= verdict false-attribution discipline) を draft 時に意識的に適用、 `verified` → `inconclusive` に corrective re-attribution。 自動化候補: driver function 名を `_check_routing()` 等に rename、 analyst 判定を別 layer に。

---

## 3. 強化 / 新確立された原則

### 原則 4 (= 観測 infra) の active operationalization
`llm_replay.py --patch` を passive な tool reference から **active なdebug loop** へ昇格。 dogfood findings draft 前に必ず適用すべき discipline。 memory `feedback_iterative_replay_patch_disambiguation.md` で operationalize、 user 明示 validation。

### 並列 sub-agent dispatch の isolation pattern (= dogfood-discipline §6.6 multi-shot 拡張)
worktree isolation は code-only、 session 隔離は per-reyn-agent 名 + per-cwd workspace で達成。 multi-process reyn instance 完全独立。 memory `feedback_dogfood_parallel_reyn_agent_isolation.md` で operationalize。

### 原則 10 (= structural pre-check) の extension
従来 「wiring 確認」 中心だったが、 batch 23 で **env-dependent default audit** も必要と判明 (= S3 sandbox.backend、 SP project_context_path)。 prelude template に config-default verification を必須化。

### 原則 12 (= verdict false-attribution) の自動化候補
driver verdict 関数を mechanism check / intent check で 2 layer 分離する設計が batch 24+ で重要。 S3 で 「verified (routing)」 と 「inconclusive (intent)」 を明示分離した経験を template 化。

### 原則 14 (= scenario design audit 4-dim) は依然 valid
S1 / S2 / S3 共に 4 dim audit ✓ で execution、 audit miss はなく、 prediction miss は **prelude の prediction model side** に bias。 audit は scenario 設計レベルで working、 prediction calibration は別軸。

---

## 4. 次 batch (= batch 24 core path verification N=3) への申し送り

### 必須前作業
1. **driver isolation pattern**: per-cwd + per-reyn-agent (= `b24_s<N>`) を standard 化、 driver の `--agent-name` flag 使用
2. **sandbox.backend** を batch 24 scenarios で明示 (= reyn.local.yaml override or env-based control)
3. **prompt-structure 軸** を prelude prediction model に追加: P-explicit-AND / P-explicit-SEQ / P-natural-AND / P-natural-SEQ
4. **detect_attractor.py CLI** doc sync (= `--trace` が正、 `--root` ではない)

### Scenario candidates (= N=3 per scenario)
- **S1-A (= P-explicit-AND parallel-tolerant)**: 同 prompt、 verdict logic を 「parallel + correct error surface = verified」 に refine
- **S1-B (= P-explicit-SEQ baseline)**: sequential connector 言語で 3-turn expected
- **S2 retest (= N=3 stability)**: 同 prompt、 routing_decided event の chain_id 連続性確認
- **S3-noop**: explicit `sandbox.backend: noop` override で empty variant 確認
- **S3-auto**: default で 1 item + describe path
- **S4 hot-list cold start**: freq=0 から start で direct alias 呼出 rate
- **S5 search_actions semantic**: P-natural prompt で embedding-based search 誘発

### Brier target
- **0.3-0.5** (= practice batch 0.948 から calibration framework 稼働後の expected band)
- 改善 driver: dual-intent prompt の parallel 想定 + config default audit + verdict 2-layer

### 確立しなかった事項 (= carry-over)
- **Class A cognitive-bias の wrapper-only context 一般化** (= batch 23 1 instance、 multi-scenario で base rate 確認必要)
- **Class B / C attractor の wrapper-only base rate**
- **hot list direct alias 呼出 rate** (= usage accumulation 後)
- **search_actions 経路の actual 呼出 trigger 条件**

---

## 5. Cost summary

| Item | Wall-clock | LLM cost (est) |
|---|---|---|
| Pre-flight (= reyn.yaml audit / proxy verify / state cleanup) | ~5 min | $0 |
| Initial sub-agent dispatch (= worktree、 contamination) | ~3 min | ~$0.01 |
| Mid-batch pivot (= per-cwd driver lift) | ~5 min | $0 |
| Re-dispatch 3 isolated sub-agents | ~3 min | ~$0.01 |
| llm_replay --patch hypothesis testing (= 7 × N=5 ≈ 35 calls) | ~3 min | ~$0.01 |
| Synthesis (= findings + retrospective draft) | ~20 min | $0 |
| **Total** | **~40 min** | **~$0.03** |

practice batch としては cost-efficient (= prelude 想定 wall-clock 0.5h と同等)。 mid-batch pivot 込みで 40 min は acceptable。

---

## 6. Conclusion

batch 23 は:

1. **FP-0034 wrapper-only infra の structural pass** (= S2 P6 event verified、 SP refactor の 2624 chars baseline 確認)
2. **mid-batch infrastructure lift** (= worktree → per-cwd + per-reyn-agent isolation pattern)
3. **llm_replay.py --patch を active debug loop として確立** (= 原則 4 active 適用、 user 明示 validation)
4. **prelude prediction model の 2 systemic bias surface** (= dual-intent prompt parallel / sandbox.backend default)
5. **Brier 0.948 baseline** (= batch 8 帯、 batch 24 で 0.3-0.5 へ収束 target)

practice batch goal (= calibration + infra 通過確認) は **gate pass**:
- ✅ ≥2 scenario で routing_decided emit 確認 (= S2 で 1 件 + 過去 contaminated run で 1 件)
- ✅ CRITICAL finding ゼロ
- ⚠️ blocked rate 0% だが原則 12 補正で 「S2 真の blocked」 は in-flight resolution (= isolation 修正で消滅)

batch 24 へ ready: 必須前作業 4 件 + scenario set 7 candidates + Brier 0.3-0.5 target。
