# B5-M2 Root Cause Investigation: phase_retry on plan/apply_improvements

| Field | Value |
|---|---|
| Status | investigation complete |
| Source | plan_improvements.md / apply_improvements.md / batch 5-6 WAL / OS source |
| WAL note | B5 batch `.reyn/` cleaned; B6-S1 WAL available at `2026-05-04T142551_skill_improver.jsonl` (prepare retries only; plan/apply not reached in B6-S1 due to earlier path failure). Observations from B5-B narrative + OS source code analysis. |

---

## 観測

### B5-B (batch 5) — WAL から確認済み retries

B5-B-skill-improver-chain.md の narrative より:

```
[T+26.0s] phase_started: plan_improvements
[T+28.0s] phase_started: apply_improvements
```

- `plan_improvements`: phase_retry × 1 ("structurally invalid Control IR on first try")
- `apply_improvements`: phase_retry × 1 ("structurally invalid Control IR on first try")
- B5-B 文書: "The model emits structurally invalid Control IR on first try, requiring retry. This is a prompt or output-format compliance issue in those phases."

### B6-S1 (batch 6) — prepare で観測した retry pattern (参照用)

prepare phase の retry 2 件の具体的な error message は WAL から確認済み:

- attempt 1: `Artifact data validation failed for 'improvement_session': 'case_input': None is not of type 'string'; ...` → **artifact schema validation failure** (LLM が None を出力)
- attempt 2: `decide-turn output missing required 'control' block` → **control block 欠落**

plan/apply_improvements の phase_retry も同パターンである可能性が高い (同一モデル / 同一 OS validation path)。

### OS validation path (kernel/runtime.py 確認)

phase_retry が発火する経路は 2 つ:

1. `_run_decide_with_retry` 内: normalization / ControlIR validation / artifact schema validation 失敗
2. `_run_act_loop` 内: act budget 超過後 force_decide でも act turn を出し続ける

`apply_improvements` は `max_act_turns: 1` を宣言しており、**act budget 関連 retry も潜在的に発生しうる**。

---

## 仮説 list (= 1 仮説 1 検証 cycle 用)

### 仮説 1: `plan_improvements` の decide turn で LLM が `control` block を省略する

**evidence:**
- B6-S1 prepare の attempt 2 で "decide-turn output missing required 'control' block" が WAL に記録済み。同一モデル (gemini-2.5-flash-lite) / 同一 OS path。
- `plan_improvements` は act turns (file glob / read) を複数発行してから decide turn を出す設計。act turn 結果を受け取った後の decide turn で LLM が `{"type": "decide", "artifact": {...}}` と出力し `control` を省略するケースが gemini-2.5-flash-lite で観測されている。
- `normalizer.py:146-149`: `control_raw = raw.get("control"); if control_raw is None: raise ControlIRValidationError("decide-turn output missing required 'control' block")`

**確度: 高**

**検証コスト: cheap** — `plan_improvements` の実行ログを 1 run 取得し、retry attempt 1 の raw response を `llm_response_received` event から確認する。WAL の `llm_response_received.raw` に `control` key があるか確認するだけ。

---

### 仮説 2: `apply_improvements` の `max_act_turns: 1` 制約下で LLM が act turn を超過し、force_decide retry が発生する

**evidence:**
- `apply_improvements.md`: `max_act_turns: 1` を明示宣言。"Issue ALL file ops ... in a single act turn" と指示しているが、Step 1 (DSL changes: N 件の write) + Step 2 (improver_state.json read + write) を **1 act turn に収める**制約がある。
- `runtime.py:946-966`: act budget 超過後も LLM が act turn を出した場合 `force_decide` retry が発生。
- B5-B では `improvement_plan.changes` が N 件 (file write × N + state read + state write) となるケースがある。LLM が「すべてを 1 turn に収める」指示を守れずに 2 act turn 目を試みる可能性。
- `apply_improvements.md`: "CRITICAL: Issue Steps 1 and 2 ops together in the SINGLE act turn." — 明示されているが weak LLM が無視するパターンは B5-H1 で既出。

**確度: 中**

**検証コスト: cheap** — WAL の `act_executed` events で `apply_improvements` の act turn count を確認。2 件 `act_executed` があれば force_decide retry が発生している。

---

### 仮説 3: `apply_improvements` の decide turn (finalize path) で `improvement_result` スキーマ (12 required fields) の artifact validation failure

**evidence:**
- `improvement_result.yaml`: required fields が 12 個 (`target_skill_path`, `iterations_performed`, `initial_score`, `final_score`, `score_history`, `files_modified`, `termination_reason`, `summary`, `next_steps`, `work_dsl_root`, `original_dsl_root`, `copied_back`)。
- `apply_improvements.md`: finalize path では `_resolved_paths` から複数フィールドを読み出して artifact を構築する。LLM が `_resolved_paths` を参照せず path を null / 空文字で出力すると schema validation failure。
- B6-S1 prepare attempt 1 の error が "None is not of type 'string'" — 同一パターン (path フィールドを None で出力) が apply_improvements の finalize path でも起こりうる。
- `improvement_session.yaml` では `_resolved_paths` は `session` 内のフィールドだが、`iteration_state` → `session._resolved_paths` という深いネストを LLM が正確に辿れない場合がある。

**確度: 中**

**検証コスト: cheap** — WAL の `validation_error` event で `phase=apply_improvements` / `error` の内容を確認。"None is not of type" が含まれていれば仮説 3 確定。

---

## 推奨 fix order

仮説 1 から順に test (確度高 + 検証 cheap 順):

1. **仮説 1** (control block 省略): 次回 plan/apply_improvements が到達する run の WAL で `llm_response_received.raw` の `control` key 有無を確認。確認されたら phase instructions に "ALWAYS include the top-level `control` block in your decide turn — the OS rejects responses that omit it" を追記。
2. **仮説 2** (act turn 超過): 同 WAL で `act_executed` count を確認。2 件あれば `apply_improvements` の instructions に "All ops MUST fit in ONE act turn (max_act_turns=1)" の visible reminder を先頭に移動。
3. **仮説 3** (12-field artifact failure): 同 WAL で `validation_error` の error 内容を確認。None fields が原因なら `apply_improvements` に `_resolved_paths` の参照方法の具体例を追記。

各仮説は独立して検証可能。仮説 1 が確認されれば fix → batch 7 retest で残り retry が消えるかを確認。消えなければ仮説 2/3 を継続。

---

## Out of scope

- `plan_improvements` の act turn 数は多い (glob × 2 + read × N) が max_act_turns の制限は宣言されていない (デフォルト 10) → act budget exhaustion は plan_improvements では起きにくい。plan 側の retry は仮説 1 (control block 省略) が最有力。
- `improvement_plan` の artifact schema は 4 required fields のみ + `changes` が array → `improvement_result` (12 fields) よりスキーマ失敗リスクは低い。plan 側のスキーマ失敗は仮説 1 確認後に再評価。
- phase instructions への schema example 追加 (P8 違反になるため、schema 自体のフィールド削減か、instruction の wording 改善のみを検討)。
- `improvement_result` の `copied_back: false` の boolean 出力忘れも軽微なスキーマ失敗候補だが、仮説 3 の主因ではなく派生。
