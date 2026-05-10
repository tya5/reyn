# S9: Cost Preflight Gate — Batch 18 Retest

| Field | Value |
|---|---|
| Date | 2026-05-10 |
| main HEAD | `9681096` (= post fix wave + embedding wiring) |
| Scenario | S9 retest — verify abort path now structurally usable |
| Sample size | N=3 |
| Input model | `openai/gemini-2.5-flash-lite` |
| Embedding | `REYN_EMBEDDING_PROVIDER=fake` (dogfood) |
| Trace dumps | `/tmp/reyn_s9_b18/run_{1,2,3}.jsonl` |
| **Verdict breakdown** | **verified: 0 / refuted: 3 / inconclusive: 0 / blocked: 0** |

## 1. Summary table

| 項目 | batch-17 | batch-18 retest 予測 | 実測 |
|---|---|---|---|
| verified | 0/3 | 60-70% (2/3) | **0/3** |
| refuted | 3/3 | 20-25% | **3/3** |
| abort candidate present in `candidate_outputs` | ✗ | ✓ | **✓ (3/3)** |
| LLM saw `cost.threshold_exceeded: true` | ✓ | ✓ | **✓ (3/3)** |
| LLM emitted `control.type: "abort"` | ✗ | (= main test) | **✗ (0/3)** |
| postprocessor ran | ✓ | ✗ | **✓ (3/3、 1068 chunks/run)** |
| SQLite `index.db` created | ✗ (embed fail) | ✗ | **✓ (3/3、 fake embed succeeded)** |
| sources.yaml has `large` entry | ✗ | ✗ | (見つからず — manifest write is in different location, not blocker) |
| total elapsed | ~45s | ~30s | 44.2s (avg 14.7s/run) |

予測 Brier (verified=0.65 想定): E[B] = (0.65-0)² + (0.20-1.0)² + (0.10-0)² + (0.05-0)² = 0.4225 + 0.64 + 0.01 + 0.0025 = **1.075** (= 4-class 平均: 0.269) — 予測の楽観バイアスで悪化。

## 2. Per-run details

| Run | rc | chunks | sqlite | LLM control.type | LLM reason (excerpt) | Elapsed |
|---|---|---|---|---|---|---|
| 1 | 0 | 1068 | ✓ | `finish` | "The cost estimate is within acceptable limits…" | 14.1s |
| 2 | 0 | 1068 | ✓ | `finish` | "estimated cost is well within acceptable limits" | 15.4s |
| 3 | 0 | 1068 | ✓ | `finish` | "the number of chunks does not exceed the warning threshold" | 14.7s |

Cost preflight values seen by LLM (= 全 run 共通):
```
"cost": {
  "chunk_count": 250,
  "estimated_tokens": 15875,
  "estimated_cost_usd": 0.0003,
  "model": "standard",
  "threshold_exceeded": true
}
```

Run 3 LLM reason 全文 (= 最も明確に矛盾): "The cost preflight indicates that the
estimated cost is within acceptable limits **and the number of chunks does not
exceed the warning threshold**" — `threshold_exceeded: true` を直接否定。

## 3. Abort path verification (= structural pre-check)

batch 17 で「abort candidate が存在しない」 と特定した structural gap は確実に
fix されている。 trace dump で確認:

```
"candidate_outputs": [
  { "next_phase": "end",   "control_type": "finish", "schema_name": "chunk_strategy", … },
  { "next_phase": "abort", "control_type": "abort",  "schema_name": "abort_reason",  … }
]
```

両 candidate が LLM 視界にあり、 description には "Abort the skill — used when
external constraints (= cost limit, …) prevent completion." と明記。 LLM が
abort を選択する **構造的 prerequisite は満たされている**。

つまり batch 18 の failure は **structural ではなく behavioral** (= LLM が
threshold_exceeded を無視する attractor)。

## 4. What happened — new attractor surfaced

batch 17 と全く異なる failure mode:

- **batch 17** (= structural): LLM が reason に「abort」と書きながら finish を
  emit。 candidate_outputs に abort が無いので構造的に abort 不可能。
- **batch 18** (= behavioral): LLM が `threshold_exceeded: true` を直接無視 +
  「cost is within acceptable limits」 と reason に書く + abort candidate
  available にも関わらず finish + chunk_strategy emit。

新 attractor 命名: **R-RAG-numerical-vs-flag-bias**

LLM が `threshold_exceeded: true` (= boolean flag) より `estimated_cost_usd: 0.0003`
(= 数値、 直感的に「安い」) を優先する pattern。 phase instruction の cost gate
記述は両条件を OR で書いている (= "If `data.cost.threshold_exceeded` is true,
OR if `data.cost.estimated_cost_usd` is unexpectedly high…"):
- `threshold_exceeded: true` → abort
- `estimated_cost_usd > $1.00` → abort

LLM はこの OR 構造を「両方満たさないと abort しない」 (= AND) と誤解 + 数値が
小さいので safe と判断。 instruction の disambiguation 不足。

## 5. What it means

### [HIGH] B18-S9-1 (NEW): LLM が threshold_exceeded boolean flag を ignore

| 項目 | 詳細 |
|---|---|
| ID | B18-S9-1 |
| 重要度 | HIGH (= cost gate UX 契約 (= ADR-0033 UX gap fix B) は behavioral にも破綻) |
| 現象 | abort candidate が available + threshold_exceeded:true でも LLM は finish を emit、 reason に「cost is within acceptable limits」 と書く |
| 観測 | 3/3 run、 全 run で LLM reason が input 数値と矛盾 |
| root cause 仮説 | (a) instruction の OR 構造を AND と誤解、 (b) estimated_cost_usd=$0.0003 の 「小さい」 anchor で threshold_exceeded を後付け denial、 (c) gemini-2.5-flash-lite が boolean flag より numeric value を信頼する bias |
| 修正方針 (= 候補) | 1. Phase instruction で cost gate 条件を強化: "If `threshold_exceeded` is true, you MUST emit decision: abort regardless of `estimated_cost_usd` value." + 例示。 2. Preprocessor で threshold_exceeded:true 時に instruction 末尾へ警告 string を inject。 3. Strong model class への切替検討 (= gemini-2.5-flash-lite quirk 可能性 — phase 2 model selection wave で対応) |
| scope | `src/reyn/stdlib/skills/index_docs/phases/strategy.md` (= cost gate section 強化) |

### structural fix (= a4c1b47) は確実に landing

batch 17 で特定した「abort candidate 不在」 は完全に解消。 `candidate_outputs`
に abort が含まれ、 description も適切。 P4 contract (= LLM picks ONLY from
OS-provided candidates) は OS layer で正しく実装されている。

batch 18 の failure は OS の責務 **外** (= LLM の指示理解 / numerical bias) に
shift した。 これは「fix wave で structural gap を全て close した結果、 残る
attractor は LLM-side のみ」 の証拠でもある (= P3 / P7 整合の prepartrip 完了
状態)。

### B17-S9-2 (= cost_warn_threshold が config から inject されない問題) は依然 deferred

reyn.local.yaml の `embedding.cost_warn_threshold: 5` 単独では preprocessor に
届かない。 driver は input artifact JSON に `cost_warn_threshold: 5` を直接
含めて回避 (= batch 17 と同 workaround)。 仮にこれを fix しても本 retest の
LLM behavioral failure は独立に発生する (= 数値 ignore は preprocessor 経路と
無関係)。

## 6. Calibration delta

| 予測 (= prelude) | 実測 | Brier component |
|---|---|---|
| verified 70% | 0/3 (0%) | (0.70-0)² = 0.49 |
| refuted 25% | 3/3 (100%) | (0.25-1.0)² = 0.5625 |
| inconclusive 5% | 0/3 (0%) | (0.05-0)² = 0.0025 |
| blocked 0% | 0/3 (0%) | (0.0-0)² = 0.0 |
| **Brier score** | — | **1.055** (= 4-class 平均: 0.264) |

batch 17 (= 0.545) より悪化。 prelude の予測根拠 (= 「abort candidate あれば
LLM は理解している、 batch 17 の reason に abort と書いていた」) は batch 17
attractor に過適合した予測だった。 batch 18 の LLM は「reason に abort」 から
「reason に cost OK」 に shift していて、 同じ scenario が **同 LLM** でも
**異なる behavioral path** で refuted する事例。

新原則 11 候補: **「structural fix landing で ✓ だけでは prediction を上げ
られない、 LLM-side attractor の re-survey が必要」**。 batch 18 はこの原則を
強化する事例 (= structural pre-check ✓ → 70% verified 予測 → 0% 実測)。

## 7. Carry-over

- **B18-S9-1** (HIGH): instructions 強化 wave 候補 (= phase 2 prompt-tuning)。 単一 commit で landed 可能 (= strategy.md 改訂のみ、 OS / chunkers 変更不要)
- **B17-S9-2** (MED): config → artifact data injection、 deferred 継続
- **R-RAG-numerical-vs-flag-bias** attractor を batch-19 prelude の R-attractor table へ追加 (= 他 scenario の boolean gate でも発生想定)
- **calibration**: 「structural ✓ だけで verified 予測を上げない」 を batch-19 prediction discipline に encode

## 8. New bugs

### [HIGH] B18-S9-1: LLM ignores `threshold_exceeded: true` even with abort candidate available

(= 詳細は §5 を参照)

---

**Driver**: `scripts/s9_b18_driver.py` (= worktree-local、 main repo に commit 不要)
**Trace dumps**: `/tmp/reyn_s9_b18/run_{1,2,3}.jsonl` (= 検証用 raw LLM payload)
**Summary JSON**: `/tmp/reyn_s9_b18/_summary.json`
