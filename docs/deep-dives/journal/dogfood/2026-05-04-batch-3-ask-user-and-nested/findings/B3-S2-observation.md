# B3-S2: ask_user e2e — observation

> 一行で: skill 名明示でも router LLM が `list_skills` を呼ばず「skill が存在しない」と
> 直接テキスト返答し、 `invoke_skill` も `ask_user` IR op も発火しなかった。
> `intervention_dispatched` イベントは 0 件。 予測通り外れ (40% 予測が的中)。

## メタデータ

| Field | Value |
|---|---|
| Scenario | S2 (ask_user e2e) |
| Date | 2026-05-04 |
| Batch | 3 |
| main HEAD | `d35c5e0` (worktree) |
| LLM | `openai/gemini-2.5-flash-lite` via LiteLLM proxy `localhost:4000` |
| Config | `reyn.local.yaml` に MCP config 追加 (`with-mcp.yaml` 相当) |
| CUI command | `reyn chat default --cui --no-restore` |
| Total LLM calls | 4 |
| Total tokens | 7,612 |
| Total cost | $0.000797 |
| Prediction | 外れ (40% 予測の外れパターン: router が pre-skill direct reply) |

---

## Action / CUI 出力 (strip ANSI)

### Turn 1

```
you > read_local_files skill を使って report.md を読んで要約して
[…] thinking…
agent> スキル `read_local_files` は存在しません。利用可能なスキルを一覧表示するには
       `list_skills("")` を呼び出してください。
```

### Turn 2 (ask_user 想定回答)

```
you > README.md を読んで
[…] thinking…
agent> スキル `read_local_files` は存在しません。利用可能なスキルを一覧表示するには
       `list_skills("")` を呼び出してください。
```

**注**: Turn 2 は ask_user IR op 発火を前提とした回答として送ったが、 agent は
Turn 1 と全く同じメッセージを返した。 skill は起動しておらず intervention は
存在しないため、 Turn 2 は単に新しい user turn として router に渡された。

---

## WAL grep 結果

```
intervention_dispatched : 0件
intervention_resolved   : 0件
skill_started           : 0件
skill_completed         : 0件
tool_called (invoke_skill|read_local_files) : 0件
skill_runs/             : 存在しない
```

### イベント一覧 (events/agents/default/chat/2026-05/2026-05-04T111956.jsonl)

```
2026-05-04T11:19:56 | chat_started
2026-05-04T11:19:56 | user_message_received  ← "read_local_files skill を使って..."
2026-05-04T11:19:57 | compaction_check
2026-05-04T11:20:27 | user_message_received  ← "README.md を読んで"
2026-05-04T11:20:28 | compaction_check
2026-05-04T11:20:59 | user_message_received  ← ":cost"
2026-05-04T11:20:59 | compaction_check
2026-05-04T11:21:02 | user_message_received  ← ":quit"
2026-05-04T11:21:05 | compaction_check
```

`tool_called` イベントが 1 件も存在しない。 router LLM は毎回テキスト返答を
選択しており、 `list_skills` すら呼んでいない。

---

## cost 確認

```
LLM calls  : 4
Tokens     : 7,612 (turn 別: 1807 / 1869 / 1934 / 2002)
Cost USD   : $0.000797
```

cost > 0 を確認 (F4 教訓クリア)。 ただし `:cost` コマンドが CUI モードでは
機能しないことを発見 (後述 new finding)。

---

## 判定

| 観測ポイント | 期待 | 実際 | 判定 |
|---|---|---|---|
| `intervention_dispatched` event | 出る | 0 件 | NG |
| CUI に clarifying question | 日本語で具体的 | 「skill が存在しない」 | NG |
| `intervention_resolved` event | user 回答後出る | 0 件 | NG |
| 最終的に README.md 要約 | user に届く | 届かない | NG |
| cost > 0 | はい | $0.000797 | OK |

**総合**: 予測「外れ」。 外れパターンは B2-INFO と同一構造だが、 今回は
「router が pre-skill clarification に逃げる」 ではなく
「router LLM が tool を呼ばずテキスト返答する」 という新しい失敗モード。

---

## 6 軸分析

| 観点 | 観測 |
|---|---|
| **応答品質** | 「skill が存在しない」 は事実無根 (`reyn skills` で `read_local_files` は stdlib に存在する)。 LLM が知識から hallucinate した。 |
| **意図解釈** | `read_local_files` 明示指定にもかかわらず router は `list_skills` を呼ばなかった。 skill 名明示の intent が伝わっていない。 |
| **待ち時間** | Turn 1 応答: 1.1 秒。 LLM call は 1 回のみ (tool 呼び出し無し)。 速すぎる応答 = 浅い処理の指標。 |
| **見せ方** | 内部状態の露出なし。 ただし「list_skills を呼んでください」 という LLM 内部の instruction leak がある (P7 違反ではないが UX 的に不適)。 |
| **エラー UX** | 「skill が存在しません」 は誤った情報。 user が `reyn skills` を見れば `read_local_files` が存在することに気づき混乱する。 |
| **state 整合性** | tool_called イベント 0 件。 WAL は `inbox_put/consume` のみ。 state 変化無し = 整合性は保たれているが処理が浅い。 |

---

## new finding: B3-S2-F1 — router が明示 skill 名に対し list_skills を呼ばない (MED)

### 観測

user が `read_local_files skill を使って` と skill 名を明示指定しても、
router LLM は `list_skills` ツールを呼ばずにテキスト返答した。
router はスキルが存在するかどうかを確認しないまま「存在しない」 と返答している。

`reyn skills` コマンドでは `read_local_files` は stdlib に存在する:

```
stdlib  (/...src/reyn/stdlib/skills)
  read_local_files  — Read one or more local project files via a configured filesystem MCP
```

### 根本原因の仮説

- B2-M1 (router が list_skills を呼ばない hallucination) が S2 でも再現。
  `read_local_files` は MCP に依存するため、 MCP 設定が `reyn.local.yaml` で
  行われており、 router の system prompt にはスキル一覧が注入されているはずだが、
  LLM が system prompt の skill リストを参照せずに知識から hallucinate した可能性がある。
- あるいは、 MCP config が正しく読み込まれず `read_local_files` が
  system prompt のスキル一覧に含まれていなかった可能性がある。

### Severity

**MED** — skill 名を明示しても機能しないのは意図解釈の根本問題。
B2-M1 (hallucination) の新しい再現例として記録。

### 追加観測ポイント (次回)

- `reyn chat` 起動後の system prompt に `read_local_files` が含まれているか確認
  (router_system_prompt.py のデバッグ出力を有効化)
- `--config examples/configs/with-mcp.yaml` フラグが実際には存在しないことも
  発見 (reyn chat は `--config` フラグを持たない; MCP 設定は `reyn.local.yaml`
  経由が正しい)

---

## new finding: B3-S2-F2 — CUI モードで `:cost`/`:quit` が特殊コマンドとして機能しない (LOW)

### 観測

CUI mode (`--cui`) では `/quit`, `/exit` のみが特殊コマンドとして機能する。
TUI mode 用の `:cost`, `:quit` はそのまま user message として router に送られ、
LLM が処理しようとする (budget_ledger に cost が記録される)。

WAL の 4 件の LLM call 内訳:
- Turn 1: `read_local_files skill を使って...` → 1 call
- Turn 2: `README.md を読んで` → 1 call
- `:cost` → 1 call (本来 router 不要)
- `:quit` → 1 call (本来 router 不要)

シナリオ手順書の `:cost` コマンドは TUI モード前提で書かれており、
CUI モードでは機能しない。

### Severity

**LOW** — dogfood rig の問題 (手順書の誤記)。 CUI モードでは `/quit` を使うべき。

---

## 事前 prediction との照合

予測 (40% 当たり):
- 外れ典型: 「router が pre-skill clarification に逃げて IR op 未発火」

実際の外れパターン:
- router は clarifying question を pre-skill で返したのではなく、
  skill が存在しないと hallucinate してテキスト返答した。
- B2-INFO と同じ「skill 未起動」の結果だが、 原因が異なる:
  - B2-INFO: router が path を確認するため pre-skill clarification
  - B3-S2: router LLM が skill 名を tool で確認せず hallucinate

`ask_user` IR op の e2e 観測は引き続き未達。 batch 4 での再試みが必要。

---

## 次回向け改善提案

1. **MCP 設定確認**: `with-mcp.yaml` の内容を `reyn.local.yaml` に merge して
   `read_local_files` が system prompt に含まれることを確認してから実行する。
2. **skill 名確認**: `reyn skills` で存在を確認済みの skill 名を使うか、
   `direct_llm` 等の常に存在する skill を使う。
3. **CUI コマンド修正**: `/quit` を使用。 cost は budget_ledger から直接確認。
4. **ask_user 誘発戦略**: skill が起動した後に ask_user が発行される状況を作る必要がある。
   `read_local_files` を path 確認フェーズで止まらせるため、 空の path や
   存在する prefix (`src/`) を渡して `ask_user` op を誘発するシナリオを設計する。
