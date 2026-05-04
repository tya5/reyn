# B4-retest S1 + S2 — Observation

> Batch 4 retest: B3-H1 (`48676ad`) + B3-M1 (`d8328b2`) fix 後の S1・S2 再実行。
> main HEAD `066d28d`、 worktree `.claude/worktrees/agent-a4fce3641d853c784`。

## メタデータ

| Field | Value |
|---|---|
| Date | 2026-05-04 |
| Batch | 4 (retest) |
| main HEAD | `066d28d` |
| LLM | `openai/gemini-2.5-flash-lite` via LiteLLM proxy `localhost:4000` |
| Fixes tested | B3-H1 (`48676ad`) + B3-M1 (`d8328b2`) |
| Tool | pexpect, CUI mode (`--cui --no-restore`) |

---

## Scenario 1 (S1): multi-agent specialist — カレーレシピ

### Setup

```bash
rm -rf .reyn/
reyn agent new specialist
reyn topology show _default  # members: default, specialist ✅
export OPENAI_API_KEY=dummy
reyn chat default --cui --no-restore
```

### Action

```
you > specialist エージェントに「カレーの簡単な作り方」を聞いて教えて
```

### CUI 出力

```
[…] thinking…
[…] dispatched 1 async request; awaiting peer reply
agent> Could not get a result from agent 'specialist'
       (reason: router completed without producing a text reply).
```

### WAL / Events grep 結果

**default agent events** (`2026-05-04T115141.jsonl`):

```
chat_started
user_message_received   ← "specialist エージェントに..."
tool_called             → list_agents(path="")
tool_returned           ← [{cluster: "default", count: 1}]
tool_called             → describe_agent(name="specialist")
tool_returned           ← {name: "specialist", role: ""}
tool_called             → delegate_to_agent(to="specialist", request="カレーの簡単な作り方を教えてください。")
agent_message_sent      kind=agent_request  ← specialist に dispatch
tool_returned           ← {status: "dispatched"}
compaction_check
agent_response_received ← from specialist
peer_reply_failed_surfaced  reason="router completed without producing a text reply"
chat_stopped
```

**specialist agent events** (`2026-05-04T115145.jsonl`):

```
chat_started
agent_request_received      ← from default
tool_called  → list_skills(path="foods/recipe")  ← [] (空)
tool_called  → list_skills(path="foods")          ← [] (空)
tool_called  → list_skills(path="")               ← [{category:"general", count:10}]
tool_called  → list_skills(path="general")        ← [direct_llm, eval, eval_builder, ...]
tool_called  → invoke_skill(name="direct_llm", input={"type":"string","data":"カレーの簡単な作り方を教えてください。"})
skill_run_spawned           run_id=20260504T025150Z_direct_llm_ca42
skill_run_completed         status=finished
tool_returned   ← {status:"finished", data:{response:"はい、簡単なカレーの作り方をご説明します。..."}}
agent_message_sent  kind=agent_response
chat_stopped
```

**specialist skill_narrator events** (`2026-05-04T115155_skill_narrator.jsonl`):

```
workflow_started
phase_started  narrate
llm_response_received  → reply_text="はい、簡単なカレーの作り方をご説明しました。材料と手順をご確認ください。"
workflow_finished
```

**観測 grep 結果** (B3-H1 fix 効果確認):

```
invoke_skill 呼び出し : 1件 ✅  (B3-H1 fix が specialist 側で機能)
describe_skill       : 0件 (不要 - list_skills → invoke_skill 直結)
list_skills          : 4件 ✅  (proper catalog browse)
skill_phase_advanced : 0件 (direct_llm は 1-phase skill)
peer_reply_failed_surfaced : 1件 ✗ (新規 HIGH bug B4-H1 による)
```

---

## S1 判定: partial (B3-H1 fix 効果あり / 新規 HIGH 発見)

| ポイント | 期待 | 実際 | 判定 |
|---|---|---|---|
| specialist が list_skills を呼ぶ | ✅ | 4回呼んだ | ✅ |
| specialist が invoke_skill を呼ぶ | ✅ | direct_llm を invoke | ✅ **B3-H1 fix 効果確認** |
| direct_llm がカレーレシピを生成 | ✅ | 生成完了 | ✅ |
| skill_narrator が reply_text を生成 | ✅ | "はい、簡単なカレーの作り方を..." | ✅ |
| user にカレーレシピが届く | ✅ | 届かず | ✗ **B4-H1 [HIGH] 発生** |
| peer_reply_failed_surfaced | なし | 発生 | ✗ |

**B3-H1 fix 効果: ✅ partial** — specialist の RouterLoop が `list_skills` 後に
`invoke_skill` まで進み、 curry recipe 生成まで成功。 B3-H1 の直接 fix は機能。

**ただし** skill_narrator の `reply_text` が `_router_loop_agent_replies` に
捕捉されず、 default に「router completed without producing a text reply」
として返った。 これは **B4-H1 [HIGH] 新規 finding**。

---

## 新規 Finding: B4-H1 [HIGH]

### タイトル

`_run_skill_awaitable` が narrator 結果を `_put_outbox` (private) 経由で
push するため `_router_loop_agent_replies` に捕捉されない

### 観測

specialist の RouterLoop が `invoke_skill` を完了し、 narrator が
`reply_text = "はい、簡単なカレーの作り方をご説明しました..."` を生成。
にもかかわらず、 default agent は `peer_reply_failed_surfaced` を発行した
(reason: "router completed without producing a text reply")。

### 根本原因

`src/reyn/chat/session.py` の `_run_skill_awaitable()` 内でナレーター結果を
送出するコード (line ~2984):

```python
await self._put_outbox(OutboxMessage(kind="agent", text=narrated, meta=meta))
```

`_put_outbox` (private) は `_router_loop_agent_replies` を更新しない。
`_router_loop_agent_replies` を更新するのは `put_outbox` (public / RouterLoopHost interface):

```python
async def put_outbox(self, *, kind: str, text: str, meta: dict) -> None:
    await self._put_outbox(...)
    if kind == "agent" and text:
        ...
        if self._router_loop_agent_replies is not None:
            self._router_loop_agent_replies.append(text)
```

`_run_skill_awaitable` は private `_put_outbox` を呼ぶため、
narrator の `reply_text` が agent_replies リストに入らない。
RouterLoop 終了時に `agent_replies` が空 → `_no_reply_marker` を生成。

### 修正案

`_run_skill_awaitable` 内の narrator 送出 (~line 2984) を
`_put_outbox` から `put_outbox` (public) に変更:

```python
await self.put_outbox(kind="agent", text=narrated, meta=meta)
```

ただし `put_outbox` は `_append_history` も呼ぶため、
`_run_skill_awaitable` で既に `_append_history` を呼んでいる場合は
二重追加に注意が必要。 コード確認が必要。

### Severity

**HIGH** — specialist が invoke_skill まで進んでも user に結果が届かない。
B3-H1 fix 後の残存バグ。 multi-agent specialist 経路が完全 broken。

### 影響

- specialist → invoke_skill → skill 完了 → narrator 生成 すべて成功するが
  default agent の routing で `peer_reply_failed_surfaced` が発生し
  user には error message のみ届く
- B2-H2 の `peer_reply_failed_surfaced` 経路 (error surfacing) は正常動作 ✅
  (silent absorption は解消済み)

---

## Scenario 2 (S2): ask_user e2e — read_local_files

### Setup

```bash
rm -rf .reyn/
# reyn.local.yaml に MCP config を追加 (with-mcp.yaml 相当)
reyn chat default --cui --no-restore
```

注: `reyn chat` に `--config` フラグは存在しない。 MCP config は
`reyn.local.yaml` 経由が正しい (B3-S2 observation で確認済み)。

### Action

```
you > read_local_files skill を使って report.md を読んで要約して
```

### CUI 出力 (最終試行)

```
[…] thinking…
agent> skill の名前が間違っています。read_local_files という名前の skill はありません。
      report.md の内容を読み取るための skill を見つけたいですか？
```

### WAL / Events grep 結果

**Trial A** (`2026-05-04T115922.jsonl`): B3-H1 fix 部分効果あり

```
tool_called  → list_skills(path="read_local_files")  ← [] (空 - MCP 未設定のため?)
tool_called  → list_skills(path="")                  ← [{category:"general", count:10}]
# セッション途中終了 (invoke_skill まで到達せず)
```

**Trial B** (`2026-05-04T115950.jsonl`): fix 効果なし

```
# tool_called イベント 0件
# router LLM が tool を呼ばず直接 text reply
```

**観測 grep 結果**:

```
intervention_dispatched : 0件 ✗
intervention_resolved   : 0件 ✗
invoke_skill (read_local_files) : 0件 ✗
ask_user IR op          : 未観測 ✗
```

---

## S2 判定: partial / ✗ (B3-M2 は likely fix されず)

| ポイント | 期待 | 実際 | 判定 |
|---|---|---|---|
| router が list_skills を呼ぶ | ✅ | Trial A では呼んだ / Trial B では呼ばず | partial |
| router が invoke_skill を呼ぶ | ✅ | 0件 (どちらも) | ✗ |
| ask_user IR op が発火 | ✅ | 0件 | ✗ |
| intervention_dispatched | ✅ | 0件 | ✗ |
| user に clarifying question | ✅ | なし | ✗ |

**B3-M2 likely fix 効果: ✗** —

Trial A では B3-H1 fix の効果で `list_skills` 呼び出しが観測されたが、
`read_local_files` が catalog に見つからず (MCP 設定不備の可能性)、
`invoke_skill` まで到達しなかった。

Trial B では fix 効果がなく router LLM が直接 text reply した (LLM 分散)。

B3-M2 の根本は「skill 名明示でも invoke_skill まで到達しない」だが:
1. list_skills を呼ぶ動作: B3-H1 fix で部分改善 (Trial A)
2. invoke_skill まで到達: 未改善 (どちらも)
3. ask_user IR op: 依然 dark (skill 起動前段階で止まる)

**追加観測**: `list_skills("read_local_files")` が空を返した点が重要。
`read_local_files` は MCP filesystem server を必要とするため、
catalog に MCP 未設定時は現れない可能性がある。 MCP config を
`reyn.local.yaml` に追加したにもかかわらず空だったのは、
MCP servers の初期化が list_skills の catalog に反映される条件を確認する必要がある。

---

## B3-H1 fix 効果 総合評価

| 対象 | fix 内容 | 確認結果 |
|---|---|---|
| specialist の `list_skills → stop` attractor (B3-H1) | list_skills 後に invoke_skill か describe_skill を呼ぶ rule 追加 | ✅ specialist が 4回 list_skills → invoke_skill まで到達 |
| default の `list_skills → direct reply` attractor (B3-M3) | 同上 | partial (Trial A で list_skills → 次ステップ移行観測、 Trial B では効果なし) |
| router が skill 名明示でも tool 呼ばない (B3-M2) | 間接的に同 rule で改善見込み | partial (LLM 分散 — Trial A 改善 / Trial B 無効) |

**結論**: B3-H1 fix は specialist 側では完全に機能し `invoke_skill` まで到達した。
しかし `_run_skill_awaitable` の private `_put_outbox` 呼び出しバグ (B4-H1) により
ユーザーへのデリバリーが依然失敗。 「カレーレシピが届く」 という観点では
**fix は機能したが上位層の別バグにブロックされた** という状況。

---

## 事前 prediction の当て率

| Scenario | 予測 | 実際 | 当て率 |
|---|---|---|---|
| S1 (curry) | 70%でカレー届く | 届かず (B4-H1) | **外れ** (ただし fix 自体は機能) |
| S2 (ask_user) | 40%でask_user観測 | 未観測 | **当たり** (40%予測の外れパターン的中) |

prediction 的中: **1/2** (S2 は外れを予測 → 的中)。

S1 は「fix は効いたが上位層で別バグ」という典型的な cascaded bug パターン。
B3-H1 fix ログによれば invoke_skill 成功まで確認できており、
fix の目的 (list_skills → invoke_skill) は達成されている。
「カレーが届かない」という最終観測は B4-H1 (別 bug) が原因。

---

## 次のアクション

1. **B4-H1 [HIGH] fix**: `session.py` の `_run_skill_awaitable` で
   narrator 結果を `put_outbox` (public) 経由で送出するよう変更。
   `_append_history` の重複呼び出しに注意。
2. **B3-M2 再確認**: MCP config が `list_skills` catalog に反映される
   仕組みを確認。 `read_local_files` が catalog に表示されない問題の
   根本を調査。 (MCP server 初期化タイミングの問題の可能性)
3. **ask_user e2e**: skill 起動が安定した後 (`direct_llm` 等の常在 skill を使って)
   skill 内部から ask_user IR op を発行するシナリオを再設計。
