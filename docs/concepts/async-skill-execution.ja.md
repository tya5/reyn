# Skill 非同期実行 (chat-mode async)

chat router が長時間実行 Skill を起動してもプロンプトが固まらない仕組みと、
完了 narration が会話 thread に LLM context を blocking せずに戻る仕組み。

## なぜ非同期か

Skill 実行は秒〜分単位かかることがあります（skill_builder / mcp_search /
eval round / 実 workload skill）。 FP-0012 までは `invoke_skill` が
chat session の main loop を **blocking** していました:

```
Session.run() main loop — sequential, single consumer
─────────────────────────────────────────────────────
kind, payload = await _consume_inbox()         ← skill 実行中はここで止まる
await _handle_user_message()
  └─ RouterLoop.run()
       └─ await invoke_skill tool
            └─ await _run_skill_awaitable()
                 └─ await agent.run()           ← 数分経過
                                                   user が 3 件入力
                                                   → 全部 inbox queue
                                                   → 何も処理されない
```

User が入力した内容は inbox に黙って溜まるだけで、 ack も progress feedback も
途中の clarifying question も不可能でした。

Chat-mode の `invoke_skill` は非 blocking 化されました。 Plan-mode は意図的に
blocking のままです (= 各 step の LLM が次 step の入力に nested skill 結果を
inline で必要)。

## chat-mode invoke_skill の現挙動

```
User: 「skill_builder で string_length を作って」
  └─ RouterLoop: invoke_skill(name="skill_builder", input={...})
       └─ _handle: spawn_skill_fn → ChatSession._spawn_skill_for_router
            └─ asyncio.create_task(_run_one_skill(...))
            └─ 即時 return:
                 {status: "spawned", run_id, chain_id, skill, note}
       └─ Router LLM が spawn ack を見て 1 文の reply を生成
  └─ Router LLM → user: 「skill_builder を起動しました — 完了したら
                         お知らせします。 進捗は /tasks で確認できます。」
  └─ Session loop: 即座に次の inbox message 処理に戻る

User: 「ちなみに recall の設定どうなってた？」
  └─ RouterLoop: recall(...) → router LLM が inline で回答
  └─ User は回答を見る — skill_builder はバックグラウンドで実行継続

[2 分後] skill_builder 完了
  └─ _run_one_skill → _enqueue_skill_completed → _put_inbox("skill_completed", {...})
  └─ Session.run() が inbox kind を pickup して
     _handle_skill_completed(payload) を呼ぶ:
        ├─ history に user-role ChatMessage を append:
        │     "[task_completed] chain_id=... run_id=...
        │      skill: skill_builder  status: finished
        │      result: {skill_name: string_length, path: ...}
        │      Please summarize for the user in 1-2 sentences."
        └─ router LLM turn を 1 回実行 (LLM は thread 全 context を持つ)
  └─ Router LLM → user: 「skill_builder が完了しました。
                          reyn/project/string_length/ に作成されました。」
```

LLM が動く 2 つのタイミング:

1. **Spawn ack** — invoke_skill が `{status: "spawned", ...}` を返したとき、
   router LLM は 1 文の ack を生成し、 進捗確認のため `/tasks` を案内します。
   同 request に対して invoke_skill を再度呼んではならず、 in-flight task に
   対する follow-up 質問もしてはいけません。
2. **完了 narration** — 会話 thread に `[task_completed]` user-role message が
   到着すると、 LLM は `result` から user-relevant fields を extract して
   1〜2 文で narrate します。

両ルールは router system prompt の `Behaviour` section で pin されています。

## なぜ fresh tool_result でなく user-role message か

OpenAI Chat Completions と Anthropic Messages API はどちらも厳格な
ペアリング制約を持っています: `tool_result` / `role: "tool"` message は
直前に対応する `tool_use` / `tool_calls` を含む `assistant` message が
必要です。 非同期 task が完了する頃には、 元の `invoke_skill` `tool_use` には
既に対応する `tool_result` (= spawn ack) が紐付いています。 同じ呼び出しに
2 つ目の result を送ると API は 400 を返します。

そのため完了は、 既存の会話 thread に挿入される合成 `user`-role message
(`meta.source = "skill_completion"`) として配送されます。 Router LLM は
thread 全 context (= 元の spawn ack + 中間の user とのやり取り + 完了通知) を
持っているので、 `chain_id` と `run_id` が specific invocation との correlation
を提供します。 並行 skill は各々の chain_id を持ち、 LLM はどの task が完了
したかを区別できます。

## エラー時の anti-optimism

Skill の terminal status は `finished` / `loop_limit_exceeded` / `error` です。
Router system prompt の narration ルール:

- `finished`: 完了確認、 必要なら次 step を hint。
- `loop_limit_exceeded`: phase budget 切れと伝え、 `safety.loop.max_phase_visits`
  を上げて再実行を提案。
- `error` / 任意の非 `finished` status / `result.error` が存在する場合:
  reply は specific error を **verbatim で surface** しなければならない。
  成功と narrate してはいけない。 user 向けに quote (output_language が設定
  されていれば翻訳、 ただし failure signal は明示維持) し、 最も可能性の高い
  fix を提案。

2026-05-10 G4 spike で、 strong tier (gemini-2.5-flash) router が
`status="error"` + `data.error` populated でも success と narrate する事例が
観測されました。 MUST-surface rule は FP-0011 Component B の強化と同時に
land し、 この flash tier optimism bias に対応します。

## Plan-mode は blocking のまま

Plan-mode RouterLoop は `run_skill_fn` のみ bind し (= 旧 blocking path)、
`spawn_skill_fn` は None のまま。 そのため plan step の LLM が `invoke_skill`
を呼んでも完了まで blocking し、 結果が次 step に inline で feed されます。
これは意図的で — plan step は sequential 実行で、 次 step の prompt は前 step
の結果を含むことが頻繁にあるため。 spawn-and-return semantics だと planner が
独自の完了追跡層を構築する必要があり、 複雑性に見合いません。

切り分けは `RouterLoop._build_router_caller_state` で wiring されています:

```python
_spawn_skill_bound = None
if hasattr(self.host, "spawn_skill") and callable(...):
    _spawn_skill_bound = ...

return RouterCallerState(
    run_skill_fn=_run_skill_bound,        # 常に present
    spawn_skill_fn=_spawn_skill_bound,    # chat-mode のみ
    ...
)
```

Plan-mode の `_PlanStepHost` は `spawn_skill` を実装しないので hasattr check が
失敗し、 binding は None のまま維持されます。

## Slash commands

`/tasks` は Skill 実行と Plan task を横断する統合 entry point:

```
/tasks                          → 実行中の全 task (skills + plans) を一覧
/tasks list                     → /tasks と同じ
/tasks status <run_id_prefix>   → current phase + 経過時間 + chain_id
/tasks kill <run_id_prefix>     → 特定 task を中止
```

旧 command も alias として継続:

- `/skill list` / `/skill discard <run_id>` — Skill のみ (PR-resume-ux U2)
- `/plan list` / `/plan discard <plan_id>` — Plan のみ (ADR-0023)

## Crash 越しに保持されるもの

| State | 配置先 | Crash 耐性 |
|---|---|---|
| inbox queue (`skill_completed` 含む) | `agents/<name>/state/inbox.snapshot.json` | yes (PR21) |
| spawned task (asyncio.Task in memory) | session 内のみ | **no** — process 終了で task も死ぬ |
| `running_skills_*` dicts | session 内のみ | no |
| 実行中の Skill state | per-skill snapshot + WAL | yes (PR-resume-auto / ADR-0023) |

Crash 前に spawn 済で完了していなかった Skill は、 標準の skill-resume 仕組み
(= per-skill snapshot + WAL replay) で resume 可能です。 chat session の
`running_skills` dict や `skill_completed` inbox には依存しません。 restore 後、
auto-resume coordinator が active な Skill を再起動し、 完了時には新規 run と
同様に restored inbox に対して `skill_completed` を enqueue します。

## 関連項目

- [コンセプト: plan-mode](plan-mode.md) — sequential step 実行
  (= 明示的に blocking、 chat-mode async と対比)
- [コンセプト: skill-resume](skill-resume.md) — in-flight skill state の
  crash recovery
- [リファレンス: chat CLI](../reference/cli/chat.md) — `/tasks` /
  `/skill` / `/plan` slash command
- FP-0011 (`docs/deep-dives/proposals/0011-remove-narrator.md`) —
  専用 narrator skill 削除、 router LLM が inline で narrate
- FP-0012 (`docs/deep-dives/proposals/0012-async-skill-execution.md`) —
  本 design の完全な proposal
