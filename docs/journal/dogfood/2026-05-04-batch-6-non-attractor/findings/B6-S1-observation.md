# B6-S1: G2 post-fix retest — observation

| Field | Value |
|---|---|
| Scenario | S1 |
| Date | 2026-05-04 |
| main HEAD | 0660bb2 |
| Prediction internal | 90% workspace dir 作成 |
| Prediction user | 70% 改善案届く |

---

## Action 入力 / CUI output

**入力**: `skill_improver で direct_llm を 1 回 review して改善案を出して`

**CUI output**: なし (0 agent messages)。pexpect timeout 内に skill chain が完了しなかったため、ユーザーへの最終 reply は届かなかった。セッション終了時点で `run_and_eval → eval(run_target)` が LLM call 中で打ち切り。

**注記**: 実行前に `.reyn/` を `rm -rf` 済みであったが、pexpect が initial prompt を取得した時点でエージェントが prior state (chain_id=9cbd7ec7) を保持していた。history.jsonl から 2 つの user message が記録されており、1 回目は S3 セッション由来の短縮入力 (`skill_improver を使って direct_llm を review して`)、2 回目が S1 正規入力 (chain_id=29bc2c6e)。S1 の観測対象は chain_id=29bc2c6e のみ。

---

## dogfood_trace 出力

### `--mode summary` (抜粋、chain_id=29bc2c6e 分)

```
[Skill Chain]  3 workflow(s) spawned for S1 input
  [T+24s] skill_improver (entry=prepare)  run_id=20260504T052551Z_skill_improver
    phases: prepare -> copy_to_work -> run_and_eval   [in-progress]
  [T+24s] skill_improver (entry=prepare)  status=active
    phases: prepare  [ask_user → user_discarded]
  [T+24s] skill_improver (entry=prepare)  status=active
    phases: prepare  [phase_retry x2 → in-progress]

[Tool Calls] (S1 chain のみ)
  router: list_skills, describe_skill
  router: invoke_skill(skill_improver) × 3  ← B5-M1 並列 invoke 再現
  skill_improver.prepare: ask_user (question: "Please provide the path to the target skill.md. For example: \"reyn/local/my_app/skill.md\".")
  skill_improver.run_and_eval: file(read .reyn/improver_state.json), run_skill(eval)
  eval.run_target: run_skill(reyn/local/my_app/skill.md) → control_ir_failed (No such file)

[Interventions]  dispatch=0  resolve=0
  ※ ask_user は dispatch されたが CUI には表示されなかった (intervention_dispatched event 未観測)
  ※ user_intervention_requested event は発行 (@ T14:25:53)

[Agent Messages]  0 message(s)
```

### `--mode chain` (S1 chain のみ、抜粋)

```
[T+24s] tool: list_skills
[T+24s] tool: describe_skill(skill_improver)
[T+24s] tool: invoke_skill(skill_improver, {suggestions:1, description:direct_llm}) × 3
  [T+24s] workflow_started: skill_improver (prepare)
    [T+26s] tool: ask_user(...)
    → user_intervention_requested / no response → run discarded
  [T+27s] phase_retry: attempt 1 (artifact validation failed)
  [T+28s] phase_retry: attempt 2 (control block missing)
  [T+29s] phase_completed: prepare -> copy_to_work
    preprocessor_step × 8 (python: compute_paths / build_copy_plan / build_write_ops / validate_copy, run_op: glob × 2, iterate × 2)
    glob reyn/local/my_app/skill.md: match_count=0
    glob reyn/local/my_app/phases/*.md: match_count=0
    _validation: {ok: false, files_written: 0, files_expected: 0}
  [T+44s] phase_completed: copy_to_work (1 LLM call) -> run_and_eval
  [T+46s] tool: file(read .reyn/improver_state.json)
  [T+46s] tool: run_skill(eval, ...)
    [T+46s] eval.run_target: run_skill(reyn/local/my_app/skill.md) → FileNotFoundError
    [T+46s] llm_called (run_target, 2nd attempt) → SESSION TERMINATED
```

### `--mode cost`

```
Total: $0.000402 | 112,082 tokens | 20 calls  (Run 1 + S1 chain 合計)
  gemini-2.5-flash-lite: $0.000402, 3,329 tokens, 2 calls (router)
  openai/gemini-2.5-flash-lite: $0.000000, 108,753 tokens, 18 calls (skill phases)
```

---

## 判定 (internal / user 分離)

| 観点 | hit/miss | 詳細 |
|---|---|---|
| internal: workspace dir 作成 | **miss** | `.reyn/skill_improver_work/` 未作成。preprocessor が glob で `reyn/local/my_app/skill.md` (0 matches) を検出し `_copy_plan=[]` → 書き込み 0 件。`direct_llm` が stdlib skill (`src/reyn/stdlib/skills/direct_llm/`) であることを prepare LLM が認識できず `reyn/local/my_app/skill.md` を補完した。 |
| internal: eval score > 0 | **miss** | eval skill が `run_target` まで到達したが `reyn/local/my_app/skill.md` への `run_skill` が `FileNotFoundError` で失敗。score 計算未到達。セッション打ち切りで eval LLM call 2 回目も未完了。 |
| internal: preprocessor 化で 0-byte write attractor 消滅 | **hit (partial)** | `copy_to_work` は preprocessor ステップ 8 段が完走し LLM に渡った (G2 fix 効果)。ただし target path 不正のため write ops = 0 → workspace dir 未作成。attractor としての 0-byte write は消えたが、write 0 という別の問題が残存。 |
| user: 改善案届く | **miss** | agent_message_sent = 0。skill chain が run_and_eval 途中で打ち切り、narrator 経由の reply 未到達。 |

---

## attractor 発生?

**yes** — G12 family (describe→stop variant 相当) ではないが、以下の挙動を観測:

1. **B5-M1 並列 invoke 再現**: router が `invoke_skill(skill_improver)` を 1 LLM call から 3 件同時発行 (chain_id=29bc2c6e でも再現)。B6-S3 と同一パターン。
2. **prepare LLM 補完エラー**: `direct_llm` という skill 名から `reyn/local/my_app/skill.md` を補完 (stdlib skill への対応なし)。これは G12 family ではなく **B6-NEW-1** 候補。

G12 attractor (= chain が途中で explain→stop するパターン) は今回未観測。

---

## 6 軸

| 軸 | 評価 | 詳細 |
|---|---|---|
| 応答品質 | NG | 改善案未到達。skill path 補完エラーが根本原因。 |
| 意図解釈 | 部分 | `skill_improver` + `direct_llm` の組み合わせは正しく解釈されたが、`direct_llm` を stdlib skill として解決できず `reyn/local/` 補完。 |
| 待ち時間 | N/A | 完走せず評価不能。copy_to_work preprocessor が 15s (T+29s〜T+44s) かかった。 |
| 見せ方 | NG | ask_user は user_intervention_requested を emit したが CUI に表示されなかった (dispatch_count=0)。 |
| エラー UX | NG | skill path 不正時のエラーが user に見えなかった。3 並列の 2 件が workflow_aborted したが router は silent に処理。 |
| state 整合性 | 部分 | WAL / events は正常記録。スナップショット保存済み (`copy_to_work` フェーズまで)。ただし打ち切り後の skill run が `active` 状態のまま残存。 |

---

## new finding 候補

| 優先度 | 種別 | タイトル | 概要 |
|---|---|---|---|
| HIGH | new bug | **B6-S1-H1: prepare が stdlib skill を `reyn/local/<name>/skill.md` に補完する** | `direct_llm` (stdlib) を指定すると prepare LLM が `reyn/local/my_app/skill.md` を補完し copy 0 件→ eval 失敗。stdlib skill の path 解決ロジックが prepare に欠落。fix: prepare instructions に stdlib skill lookup 手順を追加するか、skill resolution を OS 側で前処理して resolved_path を artifact に渡す。 |
| MED | regression obs | **B6-S1-M1: copy_to_work preprocessor 完走後も LLM が "copied" と誤認して run_and_eval へ遷移** | `_validation.ok=false / files_written=0` であるにもかかわらず LLM が「DSL files copied」と判断して continue 遷移。preprocessor validation 結果を LLM context に明示的に渡す必要あり、または validation.ok=false 時は OS 側で abort を強制。 |
| MED | observation | **B6-S1-M2: ask_user intervention_dispatched が 0 (= CUI 未表示)** | `user_intervention_requested` event は発行されたが `intervention_dispatched=0`。3 並列 instance から複数の ask_user が競合し、OS がいずれかを選択する前に他 instance の完了/abort で chain_peer_discarded が発生した可能性。 |
