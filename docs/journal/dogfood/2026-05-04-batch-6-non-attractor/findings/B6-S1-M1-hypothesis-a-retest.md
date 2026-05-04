# B6-S1-M1: 仮説 (a) `_` prefix 検証 — retest after eval_builder + B5-M2 fixes

| Field | Value |
|---|---|
| Date | 2026-05-04 |
| main HEAD | 0fd6d0b (= eval_builder fix `e6de782` + B5-M2 fix `0fd6d0b` landing 後) |
| Hypothesis | (a) underscore prefix が LLM の internal field 解釈を誘発し、validation 結果を judgment に使えていなかった |
| Verdict | **inconclusive (copy_to_work 到達したが preprocessor 失敗、LLM 未呼び出し)** |

---

## Setup

- worktree: `agent-a35d494e9c9ca8e0a` (main HEAD `0fd6d0b`)
- `reyn.yaml` に `python.trusted: allow` + `file.read: allow` + `file.write: allow` を一時追加 (dogfood 用一時設定)
- `.reyn/` はクリーン状態で 3 run 実行:
  1. `reyn chat default --cui --no-restore` (pexpect 240s) — chat mode
  2. `reyn run skill_improver --allow-untrusted-python` (run mode ×2) — trusted python 解除

## Action 入力 + dogfood_trace 出力

**入力** (全 run 共通): `skill_improver で direct_llm を 1 回 review して改善案を出して`

### Run 1: `reyn chat default --cui --no-restore` (pexpect)

**`--mode summary`:**

```
[Skill Chain]  (2 workflow(s))
  skill_improver (entry=prepare)  status=active
    phases: prepare
  skill_improver (entry=prepare)  status=active
    phases: prepare -> copy_to_work

[Tool Calls]  (4 important tool call(s))
  invoke_skill(skill_improver) ×2  caller=default
  file(write ".reyn/improver_state.json") ×2  caller=skill_improver.prepare

[Agent Messages]  0 件

=== Cost Summary ===
  Total: $0.000423  |  16,644 tokens  |  4 calls
  gemini-2.5-flash-lite: $0.000423  3,630 tokens  (2 calls)
  openai/gemini-2.5-flash-lite: $0.000000  13,014 tokens  (2 calls)
```

**`--mode chain` (抜粋):**

```
[T+28.0s] tool: invoke_skill(skill_improver) ×2
[T+28.0s] workflow_started: skill_improver
  [T+28.0s] phase_started: prepare
  [T+31.0s] tool: file(write ".reyn/improver_state.json")
  [T+31.0s] phase_completed: prepare  next=copy_to_work
  [T+31.0s] phase_started: copy_to_work
  [T+32.0s] preprocessor_step_started: step_index=0 (python, trusted)
  [T+32.0s] preprocessor_step_failed: step_index=0
  [T+32.0s] skill_run_interrupted: PreprocessorError will_resume=true
```

### Run 2 & 3: `reyn run skill_improver --allow-untrusted-python`

両 run とも同パターン:

```
phase_started: prepare
... (LLM 2-5 act turns, run_skill eval_builder が control_ir_failed)
phase_completed: prepare  next=copy_to_work
phase_started: copy_to_work
preprocessor_step_started: step_index=0 (python, trusted)
python_step_started: compute_paths (mode=trusted)
python_step_completed: compute_paths         ← Step 0 成功
preprocessor_step_completed: step_index=0
preprocessor_step_started: step_index=1 (run_op glob)
permission_denied                            ← Step 1 失敗
preprocessor_step_failed: step_index=1
```

---

## 観測

### copy_to_work 到達 — 前回との差分

前回 inconclusive の根本原因 (eval_builder が stdlib path を解決できず abort → prepare が copy_to_work に遷移不能) は**解消**された。

- chat run: `phase_started: copy_to_work` が 2 件発火 (both workflow)
- run mode: `phase_started: copy_to_work` が 2 件発火

eval_builder fix (`e6de782`) + B5-M2 fix (`0fd6d0b`) により prepare phase が copy_to_work に遷移できるようになった。

### Chat mode の失敗原因 (新規発見)

chat run では `preprocessor_step_failed` がステップ 0 で発生:

```
"error": "Phase 'copy_to_work' preprocessor step[0] python
./copy_to_work_resolver.py:compute_paths: python step
./copy_to_work_resolver.py:compute_paths declares mode='trusted'
but --allow-untrusted-python was not provided."
```

- `reyn.yaml` の `python.trusted: allow` は `startup_guard` の prompt をスキップするが、`PermissionResolver.check_python_step()` の実行時 hard-fail (`trusted_python_allowed` フラグ) を bypass しない
- `reyn chat` コマンドは `PermissionResolver` を `trusted_python_allowed=False` 固定で生成 (CLI フラグなし)
- → `reyn chat` context では trusted python step は **config 設定に関わらず常に失敗する** (設計上の gap)

### Run mode の失敗原因

`--allow-untrusted-python` フラグで trusted python (Step 0) は成功したが、Step 1 (run_op glob) で失敗:

```
"glob not permitted: '/Users/yasudatetsuya/.../sandbox_2/src/reyn/stdlib/skills/direct_llm/skill.md' (outside project)"
```

- `compute_paths` が返す `skill_glob` は **absolute path** (stdlib skill は絶対 path で resolve される)
- `Workspace.glob_files()` は absolute path の glob について `base_dir` および `state_dir` の下かを確認し、外れていれば `PermissionError` を raise (line 97-101)
- worktree の `base_dir` = `.../worktrees/agent-a35d494e9c9ca8e0a/`, stdlib path = `.../sandbox_2/src/...` → 境界外
- `file.read: allow` の config 設定は `PermissionResolver.require_file_read()` を bypass するが、`Workspace.glob_files()` の boundary check は `PermissionResolver` を経由しない (別レイヤー)

### `data.validation` フィールド名 — LLM 観測不能

両実行パターンとも **LLM call が copy_to_work phase で発生しなかった** (max_act_turns=0 で preprocessor 失敗により phase が中断)。

- `data._validation` → `data.validation` の rename (commit `3cf7412`) が LLM judgment に与える効果を直接観測できなかった
- 仮説 (a) の核心的観測 (LLM が `validation.ok` を参照するかどうか) は **この run でも未検証のまま**

---

## 仮説 (a) の verdict

**inconclusive (copy_to_work 到達したが preprocessor 失敗、LLM 未呼び出し)**

理由:
- copy_to_work phase への到達は確認 (前回の "到達不能" から改善)
- しかし preprocessor が step 0 (chat) または step 1 (run mode) で失敗し、LLM が呼ばれなかった
- `data.validation` フィールドを LLM が参照するかどうかを観測する機会がなかった
- フィールド名 rename の効果 (仮説 a の真偽) は **依然として未検証**

---

## 前回 verification との差分

| 項目 | 前回 (HEAD `3cf7412`) | 今回 (HEAD `0fd6d0b`) |
|---|---|---|
| copy_to_work 到達 | 不可 (prepare が abort) | **可** (両モードで phase_started 確認) |
| eval_builder abort | あり (stdlib path 解決失敗) | **なし** (eval_builder fix 効果あり) |
| prepare → copy_to_work 遷移 | 不可 | **可** |
| copy_to_work preprocessor step 0 (chat) | 未到達 | **失敗** (trusted python hard-fail) |
| copy_to_work preprocessor step 0 (run) | 未到達 | **成功** (--allow-untrusted-python) |
| copy_to_work preprocessor step 1 (run) | 未到達 | **失敗** (workspace 境界外 glob) |
| LLM call (copy_to_work) | 未発生 | 未発生 |

eval_builder fix + B5-M2 fix により前回の根本障害は解消された。ただし新規の 2 つの障壁が発見された。

---

## 新規発見 (本 retest で初観測)

### [新規 bug] `reyn chat` で trusted python step が常に失敗する

- `reyn.yaml` に `python.trusted: allow` を設定しても `reyn chat` context では実行時に hard-fail する
- `reyn run --allow-untrusted-python` は flag があるが `reyn chat` にはない
- skill_improver の `copy_to_work` preprocessor は trusted python step を使用するため、`reyn chat` では **設定に依存せず到達不能**
- 分類: `reyn chat` CLI の trusted python サポート欠落 (設計 gap、P3 違反候補)

### [新規 bug] stdlib skill の absolute path が workspace 境界外 glob エラーを起こす

- `compute_paths` が stdlib skill の絶対 path を返す → `Workspace.glob_files()` の境界チェックで拒否
- `file.read: allow` config は bypass されない (別レイヤーのチェック)
- 分類: workspace boundary と stdlib skill path の不整合 (copy_to_work_resolver.py と Workspace の責任境界問題)

---

## Next action

| 状況 | 推奨アクション |
|---|---|
| 仮説 (a) 観測継続が目的 | **方法 D** を採用: LLMReplay (Tier 3 test) で copy_to_work LLM call を制御、`validation.ok` 参照を直接観測 |
| chat trusted python gap | `reyn chat` に `--allow-untrusted-python` 相当 flag を追加、または `python.trusted: allow` config が runtime check も bypass するよう修正 |
| workspace boundary 問題 | `Workspace.glob_files()` に `PermissionResolver.is_read_allowed()` チェックを追加、または `compute_paths` が worktree-relative path を返すよう修正 |

前回 verify doc の「方法 D: unit test で copy_to_work preprocessor の output + LLM judgment を LLMReplay で検証」が最短経路。chat/run 経由の e2e では複数の infrastructure 障壁があり観測コストが高い。

---

`reyn.yaml` `python.trusted: allow` / `file.read: allow` / `file.write: allow` の一時追加は削除済み (この dogfood 専用、commit 対象外)。
