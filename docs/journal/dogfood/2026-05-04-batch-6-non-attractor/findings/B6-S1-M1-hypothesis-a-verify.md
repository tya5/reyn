# B6-S1-M1: 仮説 (a) `_` prefix 検証 — observation

| Field | Value |
|---|---|
| Date | 2026-05-04 |
| main HEAD | 3cf7412 (= `_validation` → `validation` rename 適用済み) |
| Hypothesis | (a) underscore prefix が LLM の internal field 解釈を誘発し、validation 結果を judgment に使えていなかった |
| Verdict | **inconclusive — copy_to_work 経路に到達不能** |

---

## Setup

- worktree: `agent-a6066edbf117074e1` (main HEAD `3cf7412`)
- `reyn.yaml` に `python.trusted: allow` を一時追加 (trusted-mode preprocessor step 許可)
- `.reyn/` をクリーン後に実行

## Action 入力 + dogfood_trace 出力

**入力**: `skill_improver で direct_llm を 1 回 review して改善案を出して`  
(B6-S1 と同じ wording)

**CUI 実行**: pexpect 240s、`reyn chat default --cui --no-restore`

### `--mode summary` (最終)

```
[Skill Chain]  (5 workflow(s))
  skill_improver (entry=prepare)  status=active  phases: prepare
  eval_builder (entry=analyze_skill) × 4  status=active  phases: analyze_skill

[Tool Calls]  31 件
  invoke_skill(skill_improver) × 1  [G3 dedupe 正常動作 — deduped × 2]
  run_skill(eval_builder) × 2  [caller=skill_improver.prepare]
  file(read "direct_llm/skill.md") × 7 × 2 = 14 + 15  [caller=eval_builder.analyze_skill]

[Agent Messages]  0 件
```

### `--mode chain` (抜粋)

```
[T+246s] invoke_skill(skill_improver)  → 1 件通過 (deduped × 2)
[T+246s] workflow_started: skill_improver (prepare)
  [T+247s] run_skill(eval_builder)    ← prepare Step 2 が eval.md 不在と判定
    [T+247s] eval_builder.analyze_skill
      file(read "direct_llm/skill.md") × 7 → act budget 枯渇
      phase_retry(attempt=1)
      control_decided: abort (direct_llm/skill.md not found)
      workflow_aborted
      control_ir_failed
  [T+259s] run_skill(eval_builder)  [2 回目]
    [T+259s] 同パターン再現 → workflow_aborted
  prepare phase: 2 act_turn 完了、skill_improver 継続 (status=active 終了)
```

### `--mode cost`

```
Total: $0.000384 | 21,887 tokens | 5 calls
  gemini-2.5-flash-lite: $0.000384, 3,363 tokens, 2 calls (router)
  openai/gemini-2.5-flash-lite: $0.000000, 18,524 tokens, 3 calls (phase)
```

---

## 観測

- validation 結果: **到達不能** (copy_to_work phase に進まなかった)
- LLM 次 turn の挙動: copy_to_work への遷移判断が観測できなかった
- WAL の関連 events:
  - `tool_call_deduped` × 2 (G3 fix 正常動作)
  - `skill_run_failed` なし (trusted python 許可後)
  - `workflow_aborted` × 2 (eval_builder が abort)
  - `control_ir_failed` × 2 (eval_builder abort の連鎖)

### copy_to_work 未到達の根本原因

B6-S1-H1 fix (`0a92db0`) で `prepare` phase の Step 2 が「eval.md が存在しない場合は `eval_builder` を呼んで生成する」という指示に変わった。`eval_builder.analyze_skill` は `direct_llm/skill.md` を相対パスで読もうとするが、`direct_llm` は stdlib skill (`src/reyn/stdlib/skills/direct_llm/`) にあり、cwd 相対パスでは存在しない。

結果: `prepare` が `eval_builder` を 2 回呼ぶが両回 abort → `prepare` が `copy_to_work` に遷移できない → `data.validation` 経路に到達不能。

---

## 仮説 (a) の verdict

**inconclusive (到達不能)**

理由: `_validation` → `validation` の変更が効くかどうかを観測する `copy_to_work` phase の実行に到達できなかった。B6-S1-H1 fix の副作用として `prepare` → `eval_builder` → abort のループが新たに発生しており、仮説 (a) の観測経路が構造的にブロックされている。

`_` prefix の効果 (仮説 a の真偽) は **未検証のまま** 残存。

---

## 新規観測: eval_builder stdlib skill path 問題

`eval_builder.analyze_skill` が `direct_llm/skill.md` (相対パス) で read を試みている。これは stdlib skill のパス解決が `eval_builder` に欠落していることを示す。

- B6-S1-H1 fix は `skill_improver.prepare` のパス解決を OS 側 (`compute_paths` trusted step) に移した
- しかし `eval_builder` には同様の OS-side path 解決がなく、LLM が相対パス `direct_llm/skill.md` を直接読もうとする
- stdlib skill は `src/reyn/stdlib/skills/<name>/` にあるため、cwd 相対パスでは見つからない

**本観測の分類**: B6-S1-H1 fix の未解決残余、または新規 bug (eval_builder が stdlib skill を参照できない)。

---

## Next action

- **inconclusive → 仮説 (a) の観測経路を別途確保してから再検証**
  - 方法 A: `eval_builder` の stdlib skill path 解決を修正してから再実行
  - 方法 B: `prepare` phase の act_turn 内で `improvement_session` を直接 emit させる controlled input を投入
  - 方法 C: `reyn run skill_improver` に `improvement_session` artifact を直接渡して `copy_to_work` を経由させる
  - 方法 D: unit test で copy_to_work preprocessor の output + LLM judgment を LLMReplay で検証
- **eval_builder stdlib skill path 問題 (新規)**: B6-S1-H1 fix の依存先として修正が必要
- **reyn.yaml `python.trusted: allow` の一時追加は削除済み** (この dogfood 専用、commit 対象外)
