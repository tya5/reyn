# B7-S1: 6 commit 統合効果 verify — observation

| Field | Value |
|---|---|
| Date | 2026-05-04 |
| main HEAD | 578bb03 |
| Scenario | S1 (6 commit 統合効果 verify: chat 経由 skill_improver 完走) |
| Verdict | **blocked** (= router LLM が skill 名を dot-notation で誤解釈、chain 未起動) |

---

## Setup

```bash
rm -rf .reyn/
# reyn.yaml に python.trusted: allow を一時追加済み (worktree-only)
export OPENAI_API_KEY=dummy
```

## Action

```bash
reyn chat default --cui --no-restore --allow-untrusted-python
```

Input: `skill_improver で direct_llm を 1 回 review して改善案を出して`

pexpect: timeout 240s, `/quit` 前に sleep 5s

## 観測

### dogfood_trace --mode summary

```
[Skill Chain]  (0 workflow(s))

[Tool Calls]  (2 important tool call(s))
  [ 1] invoke_skill({"input": {"data": {"review": "skill_improver で di...)  caller=default
  [ 2] invoke_skill({"name": "skill_improver.direct_llm", "input": {"d...)  caller=default

[Peer Failures / Chain Discards]  (0 event(s))
[Interventions]  dispatch=0  resolve=0
[Agent Messages]  (0 message(s))

=== Cost Summary ===
  Total: $0.000230  |  1,730 tokens  |  1 calls
  Per-model:
    gemini-2.5-flash-lite: $0.000230  1,730 tokens  (1 calls)
```

### dogfood_trace --mode chain

```
=== Skill / Tool Chain ===
[T+31.0s] tool: invoke_skill({"input": {"data": {"review": "skill_improver で di...)
[T+31.0s] tool: invoke_skill({"name": "skill_improver.direct_llm", "input": {"d...})
```

### dogfood_trace --mode cost

```
Total: $0.000230  |  1,730 tokens  |  1 calls
```

### WAL / events 直接観測 (events/agents/default/chat/2026-05/2026-05-04T183013.jsonl)

```
total events: 9
  chat_started
  user_message_received: "skill_improver で direct_llm を 1 回 review して改善案を出して"
  tool_call_deduped: {name: invoke_skill, reason: duplicate_invoke_skill_in_round}   ← G3 dedupe 動作
  tool_called: invoke_skill(name="skill_improver.direct_llm", ...)
  tool_failed: ValueError: skill 'skill_improver.direct_llm' not found
  tool_called: invoke_skill(name="skill_improver.direct_llm", ...)   ← deduped 後の実行
  tool_failed: ValueError: skill 'skill_improver.direct_llm' not found
  compaction_check: {outcome: too_few_turns}
  chat_stopped
```

CUI reply (observed via pexpect):
```
agent> Tool call failed (skill_improver.direct_llm: ValueError: skill
'skill_improver.direct_llm' not found; available: ['direct_llm', 'eval',
'eval_builder', 'judge_phase', 'mcp_search', 'read_local_files', 'skill_builder',
'skill_importer', 'skill_improver', 'word_stats_demo']).
Please try a different approach or rephrase the request.
```

## 6 軸評価

| 軸 | 評価 | 詳細 |
|---|---|---|
| 応答品質 | NG | 改善案未到達。chain 未起動。エラーメッセージのみ user に到達。 |
| 意図解釈 | NG | router LLM が `skill_improver で direct_llm を` を dot-notation スキル名 `skill_improver.direct_llm` と解釈。`skill_improver` に `direct_llm` を input として渡す意図が伝わらなかった。 |
| 待ち時間 | N/A | chain 未起動。router 1 LLM call (1,730 tokens) のみ。 |
| 見せ方 | 部分 | error message は user に届いた (CUI 表示あり)。ただし原因が「skill 名の誤解釈」であることは明示されない。 |
| エラー UX | 部分 | `please try a different approach or rephrase` という一般的な回復指示。具体的な rephrasing 案なし。 |
| state 整合性 | OK | WAL / events 正常記録。skill runs 未起動のため state 汚染なし。 |

## 事前 prediction 評価

scenarios.md の prediction:

```
internal metric: 60% verified / 30% inconclusive / 10% refuted
user metric: 35% verified / 45% inconclusive / 20% refuted
```

実 verdict: **blocked** (= chain 未起動、scenarios.md の verdict 4 値では `blocked`)

| 分布 | 実 verdict との対応 | hit/miss |
|---|---|---|
| internal: 60% verified | chain 完走 → blocked | miss |
| internal: 30% inconclusive | 途中停止 → blocked に近い | partial |
| internal: 10% refuted | infra fix 想定外 → 別バグ発現 | partial |

top probability category = verified (60%) vs 実 verdict = blocked → **prediction miss**。
prediction に `blocked` カテゴリを設定していなかった。分布に「別バグで chain 未起動」を表す blocked 枠を追加すべき教訓。

## verdict 根拠

router LLM が `invoke_skill` ツール呼び出し時に `name="skill_improver.direct_llm"` を使用。
これは Japanese input `skill_improver で direct_llm を` の dot-notation 誤解釈。
skill registry には `skill_improver.direct_llm` が存在しないため `ValueError` で即時失敗。
G3 dedupe は機能したが (1 tool_call_deduped 発生)、deduped 後の実行自体が失敗。
chain は一切起動せず、6 commit の累積効果 (copy_to_work / eval_builder path / trusted python flag 等) の e2e verification は **未達**。

新 finding 候補:
- **B7-NEW-1 [HIGH]**: router LLM が `skill_improver で X を` 形式の input を `skill_improver.X` という dot-notation スキル名に誤解釈する。fix 方向: skill_router の system prompt または invoke_skill ツール description に「skill 名はドット区切り不可」を明示、または input 中の日本語 `で` + 別語の組み合わせをスキル名候補から除外する前処理追加。

## next action

1. B7-NEW-1 を giveup-tracker または residuals に記録 (HIGH priority)
2. input wording を変更して再実行: `skill_improver を使って、 target は direct_llm、 1 回だけ改善案を出して` のように skill 名と target を分離する
3. または router system prompt に「`skill で target を` 形式は `invoke_skill(name=skill, input=target)` に変換すること」を追記
