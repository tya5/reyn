# S9: Cost Preflight Gate — Batch 17 Findings

| Field | Value |
|---|---|
| Date | 2026-05-10 |
| main HEAD | `62fd21b` |
| Scenario | S9 — cost preflight gate (UX gap fix B 観測) |
| Sample size | N=3 |
| **Verdict breakdown** | **verified: 0 / refuted: 3 / inconclusive: 0 / blocked: 0** |

## 1. Summary Table

| 項目 | 予測 | 実測 |
|---|---|---|
| verified | 40% (1.2/3) | 0% (0/3) |
| refuted | 40% (1.2/3) | 100% (3/3) |
| inconclusive | 15% (0.45/3) | 0% (0/3) |
| blocked | 5% (0.15/3) | 0% (0/3) |
| cost.threshold_exceeded=True 視認 | — | ✓ (3/3) |
| LLM decision=abort | — | ✗ (0/3) |
| postprocessor not run | — | ✗ (0/3) — chunks.jsonl 生成 (40 chunks/run) |
| SQLite not created | — | ✓ (3/3) — embed op 失敗で SQLite 到達せず |
| sources.yaml entry absent | — | ✓ (3/3) |
| total elapsed | — | 45.1s (avg 15.0s/run、 embed retry 込み) |

予測 Brier: E[B] = (0.40-0)² + (0.40-1.0)² + (0.15-0)² + (0.05-0)² = 0.16 + 0.36 + 0.0225 + 0.0025 = **0.545** (= 4 class 平均: 0.136)

実測 Brier: B = (0.40-0)² + (0.40-1.0)² + (0.15-0)² + (0.05-0)² = **0.545** (= 全 run refuted、 予測精度は refuted 率の保守的過小評価で悪化)

Brier delta: **−0 (exact miss)** — refuted 40% 予測に対して 100% 実測、 verified 40% 予測が 0% へ。

---

## 2. Per-Run Details

| Run | Verdict | LLM decision | LLM reason (summary) | chunks.jsonl | SQLite | sources.yaml | Elapsed |
|---|---|---|---|---|---|---|---|
| 1 | refuted | finish | "Cost preflight threshold was exceeded, so the indexing process is aborted..." | ✓ 40 lines | ✗ (embed fail) | ✗ | 15.3s |
| 2 | refuted | finish | "Cost preflight indicates threshold exceeded, aborting indexing..." | ✓ 40 lines | ✗ (embed fail) | ✗ | 14.9s |
| 3 | refuted | finish | "Cost threshold exceeded, aborting indexing to prevent excessive cost." | ✓ 40 lines | ✗ (embed fail) | ✗ | 14.7s |

**Setup** (全 run 共通):
- `.reyn/memory/file_01.md` 〜 `file_20.md`（20 ファイル、各 ~500 chars ≈ 125 tokens ≈ 1 chunk）
- reyn.yaml: `cost_warn_threshold: 5`（極小値）
- Input artifact に `cost_warn_threshold: 5` を明示（OS が config から inject しないため必要、後述）
- モデル: `openai/gemini-2.5-flash-lite` via LiteLLM proxy `localhost:4000`

**Preprocessor output** (全 run 共通):
- `data.cost.chunk_count`: 20
- `data.cost.threshold_exceeded`: `true`
- `data.cost.estimated_tokens`: 3320
- `data.cost.estimated_cost_usd`: 0.0001

---

## 3. What Happened

### 全 3 run: cost.threshold_exceeded=True を認識した上で decision=finish を emit

LLM は phase 1 context で `cost.threshold_exceeded: true` を確認し、`control.reason.summary`
に「cost threshold exceeded → abort」と明示しているにもかかわらず、**`decision: "finish"` + `artifact.type: "chunk_strategy"` を emit** した。

Run 1 LLM 出力 (全文):

```json
{
  "type": "decide",
  "control": {
    "type": "finish",
    "decision": "finish",
    "next_phase": null,
    "confidence": 1.0,
    "reason": {
      "summary": "Cost preflight threshold was exceeded, so the indexing process is aborted to prevent excessive cost."
    }
  },
  "artifact": {
    "type": "chunk_strategy",
    "data": {
      "boundary": "heading",
      "max_chunk_size_tokens": 600,
      "min_chunk_size_tokens": 50,
      "overlap_ratio": 0.1,
      "preserve_parent_context": true,
      "source": "test_large",
      "path": "/tmp/s9_dogfood/run1/.reyn/memory/*.md",
      "description": "Test files for cost preflight gate",
      "mode": "append"
    }
  },
  "ops": []
}
```

Run 2 と Run 3 も同一パターン（`boundary: heading`、 `overlap_ratio` のみ 0.0 に変化）。

### R-RAG4 attractor の根本原因: OS が abort candidate を提供していない

OS の `_build_candidates()` (`src/reyn/kernel/runtime.py` L354) は `finish` / `transition` / `rollback` のみを `candidate_outputs` に追加する。**`abort` candidate は一切 generate されない**。

LLM に届いた `candidate_outputs` (全 run 共通):

```json
[
  {
    "next_phase": "end",
    "control_type": "finish",
    "schema_name": "chunk_strategy",
    "artifact_schema": { ... }
  }
]
```

`abort` candidate が存在しないため、LLM は以下の矛盾した出力を生成する:
- `reason.summary` で「cost exceeded → abort」と記述
- しかし `control.type = "finish"` + `chunk_strategy` artifact を emit（唯一提供された candidate に従う）

これは P4 違反のシステム設計問題: 「LLM picks ONLY from OS-provided candidates」が機能するには、LLM が abort すべき状況で OS が `abort` を candidate として提供しなければならない。

### Strategy instructions の cost gate 記述は機能しているが効果がない

`strategy.md` の cost gate 指示:

```
If `data.cost.threshold_exceeded` is true, ... emit `decision: "abort"` with a clear explanation in `control.reason.summary`.
```

LLM はこの指示を **理解している**（reason.summary に「abort」と記述）が、
OS が提供する `candidate_outputs` に `abort` がないため、`finish` を選択せざるを得ない。
system prompt には「abort: unrecoverable error」の説明があるが、
`candidate_outputs` に含まれない choice は LLM には実質的に提供されていない。

### Postprocessor は LLM finish により実行された

LLM が `finish + chunk_strategy` を emit → OS が postprocessor を起動 → `apply_strategy` (python step) が `chunks.jsonl` に 40 chunks を書込み → `embed` op が `artifacts/chunks_with_vectors.jsonl` 生成を試みるが LiteLLM proxy に embedding endpoint がないため 3 retry で失敗 → SQLite への書込みは到達せず。

**つまり**: postprocessor は起動した（= cost gate が機能しなかった証拠）が、 embed op 失敗により SQLite と sources.yaml は生成されなかった。これは embedding API 不在によるセーフティネットであり、コスト制御としては不完全な結果。

---

## 4. What It Means

### [CRITICAL] B17-S9-1: OS が abort candidate を candidate_outputs に追加しない

abort が発動すべき条件 (`cost.threshold_exceeded: true`) でも OS は LLM に abort candidate を提供しない。LLM は reason で「abort」と記述しながら `finish` を emit する — 意図と行動が一致しない出力。

cost gate (UX gap fix B) の UX 契約が完全に破綻している:
- 設計意図: `threshold_exceeded` → LLM abort → postprocessor skip → zero side effects
- 実動作: `threshold_exceeded` → LLM finish (意図を reason に書くだけ) → postprocessor 実行 → chunks.jsonl 生成 → embed API 呼び出し試行

### config の cost_warn_threshold が artifact data に inject されない (追加観測)

`cost_preflight` は `data.get("cost_warn_threshold") or 10_000` で threshold を読む。
しかし OS は `reyn.yaml` の `embedding.cost_warn_threshold` を artifact data に inject しない。
本 S9 では workaround として input artifact に `cost_warn_threshold: 5` を明示したが、
本来は OS (config → artifact data injection) または preprocessor (config 直読み) で解決すべき。

---

## 5. New Bugs

### [CRITICAL] B17-S9-1: abort candidate が candidate_outputs に含まれない

| 項目 | 詳細 |
|---|---|
| ID | B17-S9-1 |
| 重要度 | CRITICAL (= 全 LLM-abort path が機能不全、 R-RAG4 attractor が structural に materialize) |
| 現象 | `_build_candidates()` が `abort` candidate を生成しない → LLM が abort が必要な状況でも `finish` を選択せざるを得ない |
| 証拠 | 全 3 run で LLM が reason に「abort」と記述しながら `decision: "finish" + chunk_strategy` artifact を emit |
| root cause | `src/reyn/kernel/runtime.py` の `_build_candidates()` L354-399: `finish` / `transition` / `rollback` のみ追加、 `abort` candidate が存在しない |
| 影響範囲 | 全 skill の abort path — cost gate (S9) だけでなく全フェーズの `decision: "abort"` が期待通り動作しない可能性 |
| 修正方針 | `_build_candidates()` に `abort` candidate を常時追加する（P7 準拠: OS-generic、 skill-specific な条件判定は不要）。 artifact schema は空 `{}` で可（rollback と同様）。 description: "Abort the workflow if an unrecoverable error has been detected. Put the reason in control.reason.summary." |
| scope | `src/reyn/kernel/runtime.py` + 関連 test (`tests/test_candidate_abort.py` 等) |

### [MED] B17-S9-2: embedding.cost_warn_threshold が artifact data に inject されない

| 項目 | 詳細 |
|---|---|
| ID | B17-S9-2 |
| 重要度 | MED (= reyn.yaml の設定値が preprocessor に届かない、 workaround で回避可能) |
| 現象 | `cost_preflight` が `data.get("cost_warn_threshold") or 10_000` を使うが、 config の `embedding.cost_warn_threshold` は artifact data に inject されない |
| 影響 | reyn.yaml に `cost_warn_threshold: 5` を設定しても preprocessor は 10,000 デフォルトを使う |
| 修正方針 | `cost_preflight` 内で config を直接読む (`from reyn.config import load_config`)、 または OS が preprocessor 実行前に artifact data へ config 値を inject する |
| scope | `src/reyn/stdlib/skills/index_docs/chunkers.py` の `cost_preflight` 関数 |

---

## 6. Calibration Delta

| 予測 | 実測 | Brier component |
|---|---|---|
| verified 40% | 0/3 (0%) | (0.40-0)² = 0.16 |
| refuted 40% | 3/3 (100%) | (0.40-1.0)² = 0.36 |
| inconclusive 15% | 0/3 (0%) | (0.15-0)² = 0.0225 |
| blocked 5% | 0/3 (0%) | (0.05-0)² = 0.0025 |
| **Brier score** | — | **0.545** (= 4 class 平均: 0.136) |

予測精度 worst case: R-RAG4 を「LLM が cost field を ignore する」と定義したが、
実際は「OS が abort candidate を提供しないため LLM は abort できない」という structural 問題だった。
behavioral fix (system prompt 強化等) では解決不可能 — OS の `_build_candidates()` 修正が必要。

次回 S9 再実施 (= B17-S9-1 修正後) の予測補正:
- verified: 60% (上方修正 — abort candidate があれば LLM は理解している)
- refuted: 20% (下方修正)
- inconclusive: 15%
- blocked: 5%
