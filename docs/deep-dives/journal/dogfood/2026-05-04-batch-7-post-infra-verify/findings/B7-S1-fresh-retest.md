# B7-S1 fresh retest — router enum + preprocessor anyOf fix 累積効果検証

| Field | Value |
|---|---|
| Date | 2026-05-04 |
| main HEAD | `eeb8ed9` |
| Original B7-S1 verdict | **blocked** (router LLM が dot-notation 誤解釈、chain 未起動) |
| Fresh retest verdict | **部分完走** (router 通過・prepare 完走・copy_to_work で permission_denied 停止) |

---

## Setup

- worktree: `agent-a742e19aaed55c4d7` (clean, main HEAD `eeb8ed9`)
- `.reyn/` を `rm -rf` で完全 flush
- `reyn.yaml` に `python.trusted: allow` を一時追加 (dogfood 専用)
- flag: `--allow-untrusted-python`
- trace dump: `REYN_LLM_TRACE_DUMP=.reyn/llm_trace_b8s1.jsonl`
- stdin piped (non-TTY mode): `sys.stdin.isatty() == False` → prompt_toolkit 不使用、readline fallback
- timeout: 380s (実際の chain 停止 T+12s、残りは attractor-like 待機)

## Action

```bash
rm -rf .reyn/
# reyn.yaml: python.trusted: allow 一時追加済み
OPENAI_API_KEY=dummy REYN_LLM_TRACE_DUMP=.reyn/llm_trace_b8s1.jsonl \
  reyn chat default --cui --no-restore --allow-untrusted-python
```

Input: `skill_improver で direct_llm を 1 回 review して改善案を出して`

pexpect 代替: subprocess + piped stdin (non-TTY) で実行。CUI mode の prompt_toolkit が
stdin.isatty() == False を検出し readline fallback に切り替わり、入力が正常に処理された。

## 観測

### dogfood_trace --mode summary

```
[Skill Chain]  (3 workflow(s))
  [2026-05-04T20:08:38] skill_improver (entry=prepare)  status=active
    phases: prepare
    run_id: 20260504T110838Z_skill_improver
  [2026-05-04T20:08:40] eval_builder (entry=analyze_skill)  status=active
    phases: (no phases recorded)
    run_id: 20260504T110840Z_eval_builder
  [2026-05-04T20:08:40] eval_builder (entry=analyze_skill)  status=active
    phases: analyze_skill -> copy_to_work
    run_id: 20260504T110840Z_eval_builder

[Tool Calls]  (8 important tool call(s))
  [ 1] list_skills({"path": ""})  caller=default
  [ 2] list_skills({"path": "general"})  caller=default
  [ 3] describe_skill({"name": "skill_improver"})  caller=default
  [ 4] describe_skill({"name": "direct_llm"})  caller=default
  [ 5] invoke_skill({"name": "skill_improver", "input": ...})  caller=default
  [ 6] run_skill({"skill": "eval_builder", ...})  caller=skill_improver.prepare
  [ 7] file({"op": "read", "path": "reyn/local/direct_llm/eval...})  caller=skill_improver.prepare
  [ 8] file({"op": "write", "path": ".reyn/improver_state.json...})  caller=skill_improver.prepare

=== Cost Summary ===
  Total: $0.001201  |  31,074 tokens  |  8 calls
```

### dogfood_trace --mode chain

```
[T+1.0s] tool: list_skills({"path": ""})
[T+2.0s] tool: list_skills({"path": "general"})
[T+3.0s] tool: describe_skill({"name": "skill_improver"})
[T+4.0s] tool: describe_skill({"name": "direct_llm"})
[T+5.0s] tool: invoke_skill({"name": "skill_improver", ...})
[T+5.0s] workflow_started: skill_improver  run_id=20260504T110838Z_skill_improver
  [T+5.0s] phase_started: prepare
  [T+6.0s] tool: run_skill({"skill": "eval_builder", ...})
  [T+7.0s] run_skill_started: eval_builder
    [T+7.0s] phase_started: analyze_skill
    [T+9.0s] control_ir_failed: PureModeViolation (analyze_skill_resolver.py)
    [T+10.0s] tool: file(op=read, path=reyn/local/direct_llm/eval.md) → not_found
    [T+12.0s] tool: file(op=write, path=.reyn/improver_state.json) → ok
    [T+12.0s] phase_completed: prepare  decision=continue  next_phase=copy_to_work
    [T+12.0s] phase_started: copy_to_work
```

### dogfood_trace --mode cost

```
Total: $0.001201  |  31,074 tokens  |  8 calls
  gemini-2.5-flash-lite: $0.001201  11,630 tokens  (5 calls)  [real LLM]
  openai/gemini-2.5-flash-lite: $0.000000  19,444 tokens  (3 calls)  [cached/no-charge]
```

### dogfood_trace --mode llm-payloads

```
[T+0.0s]  caller=router  msgs=4  tools=11  → finish=tool_calls  (1 tool call: list_skills)
[T+1.5s]  caller=router  msgs=6  tools=11  → finish=tool_calls  (1 tool call: list_skills)
[T+2.4s]  caller=router  msgs=8  tools=11  → finish=tool_calls  (1 tool call: describe_skill)
[T+3.5s]  caller=router  msgs=10  tools=11 → finish=tool_calls  (1 tool call: describe_skill)
[T+4.5s]  caller=router  msgs=12  tools=11 → finish=tool_calls  (1 tool call: invoke_skill)
[T+5.5s]  caller=phase:prepare  msgs=2  tools=0  → finish=stop  (run_skill eval_builder)
[T+9.0s]  caller=phase:prepare  msgs=2  tools=0  → finish=stop  (file read eval.md)
[T+10.0s] caller=phase:prepare  msgs=2  tools=0  → finish=stop  (file write + decide → copy_to_work)
```

LLM calls: 8 total (5 router + 3 phase:prepare)。
router は `invoke_skill(name="skill_improver")` を正しいスキル名 (enum 一致) で発行 ✅。
dot-notation hallucinate なし ✅。

### WAL 直接観測

```
.reyn/state/wal.jsonl: 12 entries
[3]  skill_started       skill_improver
[4]  skill_phase_advanced  prepare
[5-11] step_completed (prepare phase ops)
[12] step_started       copy_to_work (run_op: file read)
[14] skill_phase_advanced  copy_to_work
```

WAL に `copy_to_work` への phase_advanced は記録済み ✅。
`copy_to_work` 以降の phase (run_and_eval / plan_improvements / apply_improvements / finalize) は未到達 ❌。

### Phase 到達確認

| Phase | 到達 | 備考 |
|---|---|---|
| prepare | ✅ | 完走 (3 LLM turns) |
| copy_to_work | 部分 | preprocessor step[1] (run_op file) が permission_denied で失敗 |
| run_and_eval | ❌ | 未到達 |
| plan_improvements | ❌ | 未到達 |
| apply_improvements | ❌ | 未到達 |
| finalize | ❌ | 未到達 |

### skill_run_failed 詳細

```
Phase 'copy_to_work' preprocessor step[1] run_op (file):
  read from '.../src/reyn/stdlib/skills/direct_llm/skill.md' was not approved.
  Declare it in the skill.md frontmatter:
    permissions:
      file.read:
        - path: .../src/reyn/stdlib/skills/direct_llm/skill.md
          scope: just_path
```

`copy_to_work` preprocessor step[0] (python `compute_paths`) は成功 ✅。
step[1] (run_op file read) が stdlib path への permission_denied で失敗 ❌。
`skill_run_interrupted: will_resume=True` → プロセスは interrupt され、以降 session は attractor-like 状態で待機。

### eval_builder analyze_skill PureModeViolation

```
python_step_failed:
  module: ./analyze_skill_resolver.py
  function: compute_paths
  kind: PureModeViolation
  error: "pure mode: from-import of 'reyn.skill.skill_paths' not allowed"
```

`analyze_skill_resolver.py` が `from reyn.skill.skill_paths import ...` を実行しようとしたが、
pure mode では外部モジュールの from-import が禁止される。これは eval_builder が
`run_skill(skill=eval_builder, workspace=isolated)` で呼ばれた場合のみ発生する経路。
`--allow-untrusted-python` flag は chat 起動時に設定済みだが、`run_skill` の isolated workspace では
python.trusted が伝播しない可能性がある。

### user-visible reply

CUI 出力 (stdout) で観測:
```
[trace] [skill_improver#f30f] phase started: prepare
[trace] [skill_improver#f30f] phase started: analyze_skill  (eval_builder sub-skill)
[trace] [skill_improver#f30f] prepare → copy_to_work  (confidence=1.0)
[trace] [skill_improver#f30f] phase started: copy_to_work
[error] Router loop exceeded max iterations (5).
```

"Router loop exceeded max iterations (5)" は router が max 5 turns 以内に `invoke_skill` まで
到達したことを示す (list → list → describe → describe → invoke で 5 turn)。
これはエラーではなく、router が 5 回の LLM call を経て skill を invoke した結果の info メッセージ。

skill_run_failed の error message は user に直接届かなかった (chain 内部で処理され、
narrator への narrator_message イベントは emit されていない)。
改善案は **未到達** (finalize phase が完走しなかったため)。

## 6 軸評価

| 軸 | 評価 | 詳細 |
|---|---|---|
| 応答品質 | NG | 改善案未到達。copy_to_work で permission_denied 停止。finalize 未完走。 |
| 意図解釈 | 改善 | router が `invoke_skill(name="skill_improver")` を正しい名前で発行 ✅ dot-notation hallucinate なし。ただし router が 5 turns (list→list→describe→describe→invoke) を要した。 |
| 待ち時間 | N/A | chain 起動したが copy_to_work 停止 (T+12s)。attractor-like 待機が 360s 続いた。 |
| 見せ方 | 部分 | trace メッセージ (phase started / transition) は CUI に表示。error message は user に届かず。 |
| エラー UX | NG | skill_run_failed の詳細 error が user に見えない。permission 設定方法を user に伝える手段がない。 |
| state 整合性 | OK | WAL / events 正常記録。skill_run_interrupted が正常に emit。状態汚染なし。 |

## 旧 verdict との比較

| 項目 | B7-S1 (578bb03) | B8-S1 fresh retest (eeb8ed9) |
|---|---|---|
| chain 起動 | ❌ blocked | ✅ skill_improver 起動 |
| router invoke_skill 形式 | ❌ `skill_improver.direct_llm` (dot-notation) | ✅ `skill_improver` (正しい名前) |
| prepare phase | N/A | ✅ 完走 |
| copy_to_work | N/A | ❌ permission_denied 停止 |
| finalize | N/A | ❌ 未到達 |
| LLM calls | 1 (router only) | 8 (5 router + 3 prepare) |
| cost | $0.000230 | $0.001201 |
| verdict | blocked | 部分完走 |

**改善**: chain 起動 ✅、router dot-notation hallucinate 消失 ✅、prepare 完走 ✅。
**残ブロッカー**: copy_to_work の file.read permission_denied (= 新 finding)。

## 真因確定への寄与

### router enum fix (`9ee6ae1`) の e2e 効果

**確定**: dot-notation hallucinate (`skill_improver.direct_llm`) は消失した。
`invoke_skill(name="skill_improver")` が正しく発行されており、router enum fix は
e2e でも有効。B7-NEW-1 は **解消** された。

ただし router が 5 LLM turns (list_skills → list_skills → describe_skill ×2 → invoke_skill)
を必要とした点は残懸念。max_visits=5 の制限に到達する寸前であり、
将来的に 6+ turns が必要な状況では失敗する可能性がある。

### preprocessor anyOf fix (`3cbe983`) の e2e 効果

**部分効果**: `3cbe983` が fix した `analyze_skill` phase の anyOf compilation error は解消された
(B7-S5b の DSL compile 失敗は起きていない)。
ただし runtime で `PureModeViolation` が発生 — これは compile-time ではなく runtime の別 bug。
`analyze_skill_resolver.py` が pure mode で `from reyn.skill.skill_paths import ...` を実行しようとした。
B7-S5b fix (anyOf compile) とは独立した別 issue。

eval_builder の `analyze_skill` は prepare の act turn で `run_skill` 経由で呼ばれたが、
PureModeViolation で失敗。prepare フェーズはこの failure を処理して eval.md 読み込みに fallback した。

### B5-M2 fix の e2e 効果

`copy_to_work` preprocessor step[0] (python compute_paths) は `trusted` mode で正常完了。
B5-M2 fix (copy_to_work resolver) は e2e で有効 ✅。
step[1] (run_op file read) の permission_denied は B5-M2 とは独立した別 bug。

## 残懸念点

### [HIGH] copy_to_work の stdlib file.read permission_denied (B8-NEW-1)

**症状**: `copy_to_work` preprocessor step[1] が `src/reyn/stdlib/skills/direct_llm/skill.md`
への read を permission_denied で拒否する。
`copy_to_work_resolver.py` (step[0]) が stdlib path を正しく解決しても、
step[1] の run_op file.read が PermissionResolver にブロックされる。

**修正方向**: skill_improver の permissions frontmatter に stdlib skills path を glob で追加、
または OS が `run_op` preprocessor step の path を自動的に approval 対象にする。

### [HIGH] eval_builder analyze_skill PureModeViolation (B8-NEW-2)

**症状**: `analyze_skill_resolver.py` が pure mode で `from reyn.skill.skill_paths import ...`
を import しようとし PureModeViolation。

**修正方向**: `analyze_skill_resolver.py` を trusted mode に変更、
または reyn モジュールの from-import を避けた実装に変更。

### [MED] router が 5 turns を使い切る (attractor 境界)

router が `list_skills → list_skills → describe_skill → describe_skill → invoke_skill`
の 5 turns で max_visits=5 に到達した。このパターンが毎回再現するなら、
`reyn.yaml` の `phase.max_visits` 設定が実質的なボトルネックになる。

### [LOW] skill_run_failed の error が user に届かない

`skill_run_interrupted` / `skill_run_failed` の error text が narrator 経由で user に
伝わるパスがない。エラー UX の改善が必要。
