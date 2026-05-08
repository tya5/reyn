# B6-S2: ask_user e2e trial — observation

| Field | Value |
|---|---|
| Scenario | S2 (G5 ask_user trigger trial) |
| Date | 2026-05-04 |
| main HEAD | 0660bb2 |
| LLM | gemini-2.5-flash-lite (openai/gemini-2.5-flash-lite via LiteLLM proxy localhost:4000) |
| Prediction (internal) | 30% `intervention_dispatched` 発火 |
| Prediction (user) | 20% clarifying question が user に届く |

## Action 入力

```
read_local_files skill を使って /tmp/nonexistent_report.md を読んで要約して
```

セットアップ手順:

```bash
rm -rf .reyn/
export OPENAI_API_KEY=dummy
# reyn.local.yaml に MCP filesystem/git/fetch server + mcp permissions を追加済
reyn chat default --cui --no-restore
```

注: `reyn chat` に `--config` フラグは存在しないため、 `reyn.local.yaml` に
MCP 設定を追記して対応 (scenarios.md の `--config examples/configs/with-mcp.yaml`
は CLI で無効)。

## CUI output (agent reply)

```
agent>
```

空 reply。 skill は起動せず、 agent から実質的な出力なし。
clarifying question は表示されなかった。

## 観測 raw

### dogfood_trace --mode chain

```
=== Skill / Tool Chain ===
[T+4.0s] tool: list_skills({"path": "read_local_files"})
[T+5.0s] tool: describe_skill({"name": "read_local_files"})
```

`invoke_skill` は呼ばれず。 `workflow_started` イベントなし。

### dogfood_trace --mode summary (抜粋)

```
[Skill Chain]  (0 workflow(s))

[Tool Calls]  (2 important tool call(s))
  [ 1] list_skills({"path": "read_local_files"})  caller=default
  [ 2] describe_skill({"name": "read_local_files"})  caller=default

[Peer Failures / Chain Discards]  (0 event(s))

[Interventions]  dispatch=0  resolve=0

[Agent Messages]  (0 message(s))

[Skill Run State]  .reyn/state/skill_runs (not found)
```

### dogfood_trace --mode full (全 event 種別)

```
── chat_started (1) ──
── chat_stopped (1) ──
── compaction_check (1) ──  outcome=too_few_turns
── tool_called (2) ──       list_skills / describe_skill
── tool_returned (2) ──     同上
── user_message_received (1) ──
```

`skill_started` / `intervention_dispatched` / `intervention_resolved` / `workflow_started`
は一切発火せず。

### cost

```
Total: $0.000610  |  5,987 tokens  |  3 calls
  gemini-2.5-flash-lite: $0.000610  5,987 tokens  (3 calls)
```

LLM call 3 回 (router pre-tool + list_skills 後 + describe_skill 後)、
tool 2 回、 合計 ~10s。

## 判定

| 項目 | 結果 |
|---|---|
| `intervention_dispatched` 発火? | **no** (0 件) |
| CUI に clarifying question 表示? | **no** |
| skill が起動 (workflow_started) した? | **no** |
| `invoke_skill` 呼ばれた? | **no** |
| attractor 発生? | **yes** — G12 family: `list_skills → describe_skill → stop` |

## Prediction hit/miss

### Internal metric (30% IR op 発火)

**MISS** — `intervention_dispatched` は発火しなかった。 skill 起動段階自体が
G12 attractor でブロックされたため、 IR op 発火の前提条件が満たされなかった。

外れ予測の分類: **(c) G12 attractor で list_skills 後 invoke skip → skill 起動せず**
(scenarios.md 外れ予測 (c) に完全合致)

### User metric (20% prompt 届く)

**MISS** — clarifying question も skill 結果も user に届かなかった。
内部 miss (= skill 未起動) が user miss を連鎖的に引き起こした。

## Attractor 観測 data (G12 monitoring)

**Variant**: `list_skills → describe_skill → stop`
(= B5R2-H1 と同じ variant、 G12 tracker の既知 family)

| 観測項目 | 値 |
|---|---|
| Variant ID | `describe→stop` (G12 family v3 相当) |
| LLM model | gemini-2.5-flash-lite |
| Tool sequence | `list_skills("read_local_files")` → `describe_skill("read_local_files")` → empty reply |
| `invoke_skill` 呼ばれたか | no |
| LLM calls (router) | 3 |
| Tokens | 5,987 |
| Duration | ~10s |
| 先行 batch での同 variant | B5R2-H1 (same `describe→stop`)、 B3-H1 (`list→stop`)、 B2-H1 (`describe→stop`) |

G12 tracker に追記: S2 でも同 variant が再現。 weak LLM による `describe_skill`
後の `invoke_skill` 義務を honor しないパターンは **G12 family の最頻出 variant**
として確定。 Wave 3 G4 spike で強モデルとの比較 baseline データとして記録。

## 6 軸

- **応答品質**: skill が起動しなかったため評価不可。 agent が空 reply を返した時点で
  品質 = 0。
- **意図解釈**: router は `read_local_files` skill を正しく lookup・describe したが、
  `invoke_skill` の判断で停止。 意図解釈の途中段階まで正常、 最終 commit が欠落。
- **待ち時間**: ~10s で空 reply。 体感上の待ち時間に対して結果がゼロ — UX 最悪。
- **見せ方**: `agent>` 空行のみ。 ユーザーには何が起きたか全く伝わらない。
- **エラー UX**: skill 起動失敗に対するフィードバックなし。 B2-H2 fix (peer_reply_failed
  surfaced) はマルチエージェント経路向けで、 単一エージェント router 停止には
  適用されない。
- **state 整合性**: events は正常 (chat_started → tool_called × 2 → chat_stopped)。
  WAL / skill_runs 汚染なし。

## 後続

- **G12 monitoring data として giveup-tracker.md G12 section に追記済** (fix dispatch しない)
- **Wave 3 G4 spike の baseline として活用**: `describe→stop` variant が gemini-2.5-flash-lite
  で 3 batch 以上連続再現 → 強モデル trial の動機データとして十分
- **G5 (ask_user) は引き続き未観測**: 真の G5 観測のためには skill 起動が前提。
  G12 が解消されない限り G5 e2e は観測不能。
