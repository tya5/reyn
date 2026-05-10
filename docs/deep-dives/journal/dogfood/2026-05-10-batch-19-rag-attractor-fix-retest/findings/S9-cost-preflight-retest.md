# S9: Cost Preflight Gate — Batch 19 Retest

| Field | Value |
|---|---|
| Date | 2026-05-10 |
| main HEAD | `ef70aef` (= B18-S9-1 fix landed: strict ordered rule + boolean-wins callout) |
| Scenario | S9 retest — verify R-RAG-numerical-vs-flag-bias attractor mitigated |
| Sample size | N=3 |
| Input model | `openai/gemini-2.5-flash-lite` |
| Embedding | `REYN_EMBEDDING_PROVIDER=fake` |
| Trace dumps | `/tmp/reyn_s9_b19/run_{1,2,3}.jsonl` |
| Driver | `/tmp/s9_b19_driver.py` (= adapted from `scripts/s9_b18_driver.py`, only TRACE_BASE / WORKSPACE_BASE renamed) |
| **Verdict breakdown** | **verified: 3 / refuted: 0 / inconclusive: 0 / blocked: 0** |

## 1. Summary table

| 項目 | batch-18 | batch-19 retest 予測 | 実測 |
|---|---|---|---|
| Structural pre-check (= abort candidate present) | ✓ | ✓ | **✓ (3/3)** |
| Behavioral attractor surface rate (R-RAG-numerical-vs-flag-bias) | 100% (3/3) | 30% 残存想定 | **0% (0/3)** |
| verified | 0/3 | 65% (= 2/3) | **3/3 (100%)** |
| refuted | 3/3 | 30% | **0/3** |
| LLM emitted `control.type: "abort"` | ✗ | (= main test) | **✓ (3/3)** |
| postprocessor ran | ✓ | ✗ | **✗ (0/3)** |
| SQLite `index.db` created | ✓ | ✗ | **✗ (0/3)** |
| sources.yaml has `large` entry | (n/a) | ✗ | **✗ (0/3)** |
| total elapsed | 44.2s | ~25s | 20.9s (avg 7.0s/run) |

予測 Brier (verified=0.65 想定): E[B] = (0.65−1)² + (0.30−0)² + (0.05−0)² + (0.0−0)² = 0.1225 + 0.09 + 0.0025 + 0 = **0.215** (= 4-class 平均: 0.054). Batch 18 の 1.055 から **大幅改善** (= dogfood 史上 S9 で最大改善幅)。

## 2. Per-run details

| Run | rc | LLM control.type | postprocessor ran | SQLite created | reason summary | Verdict |
|---|---|---|---|---|---|---|
| 1 | 1 | `abort` | ✗ | ✗ | "Cost threshold exceeded: 250 chunks is over the configured limit of 5 chunks." | **verified** |
| 2 | 1 | `abort` | ✗ | ✗ | "Cost threshold exceeded: 250 chunks (threshold: 5 chunks). Estimated cost: $0.0003 USD." | **verified** |
| 3 | 1 | `abort` | ✗ | ✗ | "The number of estimated chunks (250) exceeds the configured threshold (5)." | **verified** |

> Note: driver auto-classifier mis-tagged all 3 as "inconclusive" because it
> looks for `WorkflowAborted` (PascalCase) in stderr while the OS now logs
> lowercase `workflow aborted`. Manual reclassification (= side_effects=False
> + LLM `control.type=abort` confirmed via trace dump + `rc=1` from
> `WorkflowAborted` exception) → **3/3 verified**. This is a driver hygiene
> issue, not a Reyn behavior issue.

Cost preflight values seen by LLM (= 全 run 共通、 batch 18 と同一):
```
"cost": {
  "chunk_count": 250,
  "estimated_tokens": 15875,
  "estimated_cost_usd": 0.0003,
  "model": "standard",
  "threshold_exceeded": true
}
```

## 3. What happened — R-RAG-numerical-vs-flag-bias 完全解消

Batch 18 で 100% 発生していた **R-RAG-numerical-vs-flag-bias** attractor (= LLM が
`threshold_exceeded:true` boolean flag を無視し、 `estimated_cost_usd:0.0003` 数値の
小ささを根拠に `finish` を emit) は、 batch 19 では **3/3 で完全に解消**。 LLM は
boolean flag を一次根拠として認識し、 abort path に遷移した。

特筆すべきは Run 2: LLM の reason 文に 「Estimated cost: $0.0003 USD」 を **明示的に
含めながら**、 それでも abort を emit している (= 数値を見たうえで policy flag を
優先したことの証拠)。 これは fix wave の 「Boolean policy flag wins over numeric
estimate.」 explicit callout が attractor を正面から相殺した直接的観測。

Fix landing layer の効果は **prompt-only** (= envelope / model 変更なし) で、 weak
LLM (gemini-2.5-flash-lite) でも explicit anti-attractor callout + strict ordered
rule の 2 重 reinforcement が **100% compliance** を達成した。 batch 6-12 で確立した
「explicit anti-attractor guidance は 60-80%」 base rate を上回る結果。

## 4. LLM reasoning extract (= boolean flag acknowledgement verification)

| Run | reason.summary | flag acknowledged? |
|---|---|---|
| 1 | "Cost threshold exceeded: 250 chunks is over the configured limit of 5 chunks." | ✓ (= chunks vs threshold 比較) |
| 2 | "Cost threshold exceeded: 250 chunks (threshold: 5 chunks). Estimated cost: $0.0003 USD." | ✓ (= 数値も含めつつ threshold 優先) |
| 3 | "The number of estimated chunks (250) exceeds the configured threshold (5)." | ✓ (= chunk_count vs threshold) |

3 run 全てが threshold 比較を一次根拠として記述。 Run 2 の挙動が特に重要 —
LLM は両方の値を見たうえで policy flag を優先しており、 「boolean flag は numeric
の文脈情報として読み流される」 batch 18 attractor が逆転している。

## 5. Calibration delta

| 軸 | 予測 | 実測 | delta |
|---|---|---|---|
| Structural pre-check | ✓ | ✓ | 一致 |
| Behavioral attractor base rate (R-RAG-numerical-vs-flag-bias) | 30% 残存 | 0% 残存 | **30 pp 楽観過小** (= fix 効果を過小評価) |
| verified | 65% | 100% | **+35 pp** |
| refuted | 30% | 0% | −30 pp |
| inconclusive | 5% | 0% | −5 pp |
| Brier | ~0.215 | 0.215 | 一致 |

予測 root cause: prelude で 「numerical anchoring は cognitive bias レベルで 100%
解消は phase 2 領域」 と weak-model の bias durability を根拠に 30% 残存を見込んだ
が、 **explicit named callout (= 「B18-S9-1: when boolean flag says ... but dollar
value is small ... do NOT conclude ... ignore the flag」) は cognitive bias を
相殺するに十分** だった。

新原則 13 候補: **「named anti-attractor callout (= 「B18-S9-1 のような attractor を
避けよ」 形式) は generic guidance より compliance rate が 30+ pp 高い」**。 prelude
の R-attractor table に named ID を encode することで、 fix wave が attractor 名と
1:1 対応する prompt fragment を生成しやすくなる効果も。

## 6. Carry-over

- **B18-S9-1 (HIGH)**: **closed** (= fix wave で 100% verified、 attractor 解消 confirmed)
- **R-RAG-numerical-vs-flag-bias attractor**: prior batch refuted rate 表から削除候補
- **driver classifier hygiene** (= NEW): `s9_b18_driver.py` の `_classify` が
  `WorkflowAborted` PascalCase のみ検出。 lowercase `workflow aborted` も検出に
  含めるか、 trace dump の `control.type` を一次判定にすべき。 LOW (= dogfood 内部、
  release 不影響)
- **B17-S9-2 (MED)**: config → artifact data injection 依然 deferred (= driver で
  workaround 継続)
- **calibration**: 名指し callout の効果を batch 20 prelude で R-RAG-srcread 結果と
  cross-reference (= S6 fix も named callout 形式)、 新原則 13 を観測 2 例で確定

## 7. New bugs

なし (= LLM 挙動 fix wave が想定通り landing、 新 attractor 浮上なし)。

---

**Driver**: `/tmp/s9_b19_driver.py` (= worktree-local copy of `scripts/s9_b18_driver.py`、 TRACE_BASE / WORKSPACE_BASE のみ batch 19 用に rename)
**Trace dumps**: `/tmp/reyn_s9_b19/run_{1,2,3}.jsonl`
**Summary JSON**: `/tmp/reyn_s9_b19/_summary.json`
