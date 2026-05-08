---
id: B6-S5
batch: 6
scenario: S5
date: 2026-05-04
bug_ref: B4-M1
status: observed
---

# B6-S5: eval.md path mismatch 観測

## 実行環境

- model: `openai/gemini-2.5-flash-lite` via LiteLLM proxy localhost:4000
- input: `skill_improver で direct_llm を 1 回 review して改善案を出して`
- state: fresh (`rm -rf .reyn/`)
- run timestamp: 2026-05-04T14:25:29–14:26:13

## dogfood_trace --mode full --filter file_read | grep eval.md

```
14:25:35 [142529_skill_improver] file/read phase=prepare: reyn/local/my_app/eval.md
14:25:31 [142530_skill_improver] file/read phase=prepare: reyn/local/my_app/eval.md
```

`prepare` phase が試みた eval.md path は **1 回のみ**:
- `reyn/local/my_app/eval.md` (存在しない — `my_app` は hallucination)
- B4-M1 で観測された 4 回の failed read **は再現しなかった**

## dogfood_trace --mode chain | grep -A5 prepare

```
[T+2.0s] workflow_started: skill_improver  run_id=20260504T052529Z_skill_improver
  [T+2.0s] phase_started: prepare
  [T+4.0s] tool: file({"op": "read", "path": "reyn/local/my_app/eval.md"...})
  [T+5.0s] tool: run_skill({"skill": "eval_builder", ...})   ← eval.md missing → eval_builder 起動
  ...abort: target not found

[T+3.0s] workflow_started: skill_improver  run_id=20260504T052530Z_skill_improver
  [T+3.0s] phase_started: prepare
  [T+4.0s] tool: file({"op": "read", "path": "reyn/local/my_app/eval.md"...})
  [T+5.0s] tool: run_skill({"skill": "eval_builder", ...})   ← 同上

[T+3.0s] workflow_started: skill_improver  run_id=20260504T052530Z_skill_improver_1 (別 peer)
  [T+3.0s] phase_started: prepare
  [T+7.0s] phase_completed: prepare → copy_to_work  (eval.md path: reyn/local/my_app/eval.md)
```

**観測された試行順序 (3 instance)**:
1. `reyn/local/my_app/eval.md` を 1 回だけ試み → Not found
2. eval_builder を起動して eval.md 生成試み → `reyn/local/my_app/skill.md` も not found → abort
3. 準備完了した 1 instance は hallucinated eval.md path をそのまま使って copy_to_work へ遷移

試行が deterministic かどうか: **全 3 instance が `reyn/local/my_app/eval.md` を使用** → 1 ターン内では deterministic。ただし正しい path (`reyn/local/direct_llm/eval.md`) は**一度も試みなかった**。

## prepare artifact の target_skill_path

```json
{
  "target_skill_path": "reyn/local/my_app/skill.md",
  "target_dsl_root": "reyn/local/my_app",
  "eval_spec_path": "reyn/local/my_app/eval.md"
}
```

LLM は `direct_llm` を `my_app` に hallucinate。これが B4-M1 の本質とは **別の bug** (B4-M1 は正しい target を解釈したうえで path 探索が複数回失敗する問題)。

## G12 attractor 観測 (chain summary)

```
invoke_skill(skill_improver) × 3 (並列) at T+2s  ← G12 attractor (router parallel dispatch)
  → 3 instance 全て prepare phase へ
  → 2 instance は abort (target not found)
  → 1 instance は copy_to_work → run_and_eval まで到達
  → eval.run_target で reyn/local/my_app/skill.md を require → [Errno 2] No such file
→ list_skills → describe_skill → invoke_skill(skill_improver) × 3 (再度 G12)
  → 3 instance が prepare → 2 は artifact validation error でリトライ → abort
  → 1 instance が copy_to_work (was_corrected=true) → run_and_eval → eval 起動中 (セッション終了)
```

14 workflow / 20 LLM calls / 112,082 tokens / $0.000402

## prediction hit/miss

| metric | prediction | 結果 | 判定 |
|--------|-----------|------|------|
| internal: 4 回 failed read 観測 | 80% | 観測されず (1 回 + hallucinated path) | **MISS** |
| user metric | n/a | n/a | n/a |

### MISS の理由

B4-M1 (4 回 failed read) は「正しい target を解釈したうえで `<target_dsl_root>/eval.md` を順に探索する」挙動を前提としていた。本 run では LLM が target を `direct_llm` でなく `my_app` に hallucinate したため、path search の問題以前に **target 解釈が誤っている** という別レイヤーの問題が顕在化した。

## prepare phase まで届いたか

YES — 全 3 instance が `prepare` に到達。ただし eval.md path search の観測対象 (= 複数候補パスを順に試みる動作) は**未観測**。理由: target が hallucinated なので path search 以前に abort。

## 新規観測 bug (B4-M1 fix design への影響)

**G12-NEW: target_skill_path の hallucination**  
`skill_improver で direct_llm を 1 回 review して` という入力で LLM が `direct_llm` を `my_app` と誤解釈した。これは:
- B4-M1 の path mismatch とは独立した問題
- option C (skill_improver 側が `eval_md_path` を input として受け取る) が有効な対処になる。`target_skill_path` を LLM に解釈させるのではなく、呼び出し側 (router) が解決して渡す。
- 短期 fix: `prepare` phase の instructions に「ユーザーが `direct_llm` のような skill 名を言った場合は `reyn/local/<name>/skill.md` または `src/reyn/stdlib/skills/<name>/skill.md` を試みよ」を追記

## B4-M1 fix design への提言 (本 observation から)

- option A (ADR: path convention formalize) + option B (path search 順序逆転) は引き続き有効だが、**先行して target 解釈 hallucination を fix しないと B4-M1 の再現観測自体が困難**
- 優先度: target 解釈 fix → path search order fix (B4-M1 本体)
- option C (`eval_md_path` を input field に) は上位互換解 — 検討推奨

## attractor 記録

G12 attractor 2 波:
1. T+2s: skill_improver × 3 並列 (= B5-M1 再現)
2. T+24s: 失敗後に router が list_skills → describe_skill → skill_improver × 3 再並列

fix dispatch しない (S3 観測データと合算して G3 dedupe fix 設計用 evidence とする)。
