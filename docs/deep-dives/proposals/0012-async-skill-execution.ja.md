# FP-0012: スキル/エージェント/プランの非同期実行 — 長時間タスクのノンブロッキング化

**Status**: proposed
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

スキル・エージェント委任・プランはいずれも長時間実行（数分〜数時間）を想定して設計されているが、
現状の `invoke_skill` は `await _run_skill_awaitable()` でセッションのメッセージループを
ブロックしている。スキル実行中にユーザーが打ったメッセージは inbox にキューイングされるが、
スキルが終わるまで一切処理されない。

`invoke_skill` の `_handle` がタスクをスポーンして即座に
`{"status": "spawned", "run_id": ..., "chain_id": ...}` を返すよう変更する
（`dispatch_kind` の変更は不要）。ルーター LLM はこの tool result を inline で受け取り
ユーザーへ確認応答を生成する。タスク完了時は narrator を経由せず、`chain_id` と結果を持つ
`user` ロールメッセージを**既存の会話スレッドに注入**してルーター LLM に narrate させる。
LLM はスポーン時のコンテキストを保持したまま正確に完了を narrate できる。

---

## Motivation

### ブロッキング問題

```
Session.run() メインループ — 逐次処理、コンシューマー 1 本
─────────────────────────────────────────────────────
kind, payload = await _consume_inbox()      ← スキル実行中はここでブロック
await _handle_user_message()
  └─ RouterLoop.run()
       └─ await invoke_skill ツール
            └─ await _run_skill_awaitable()
                 └─ await agent.run()       ← ここで数分待機
                                               ユーザーが 3 件入力
                                               → 全て inbox にキュー
                                               → 一切処理されない
```

5 分かかるスキルが動いている間、チャットは事実上フリーズする。
ユーザーが何を打っても無言でキューに溜まり、進捗フィードバックも介入手段もない。

### 修正コードはすでに「デッドコード」として存在する

`_dispatch_routing_decision_for_user`（呼び出し元ゼロ）は
`asyncio.create_task(_run_one_skill(...))` を使う正しい fire-and-forget パターン。
`running_skills` dict・`running_skills_started_at`・`running_skills_chain` の
インフラはすでに揃っており、`/skill list` と `/skill discard` スラッシュコマンドも
このdictに対して動作している。欠けているのは `invoke_skill` をこのパスに繋ぎ、
完了結果をルーター LLM に届ける仕組みだけ。

### 対象範囲: スキル + エージェント委任 + プラン

長時間動作する 3 種すべてがノンブロッキングであるべき。
`delegate_to_agent` はすでに `dispatch_kind="async"` 対応済み。プランもすでに
`create_task` を使っている。本 FP は唯一残っているブロッキングケース（`invoke_skill`）に
対処し、タスク管理 UX を統一する。

---

## Proposed design

### Phase 1 — invoke_skill のノンブロッキング化（spawn-and-return）

**`_handle` がタスクをスポーンして即座に return — `dispatch_kind` は変更しない:**

```python
# invoke_skill.py の _handle — バリデーション後
task = asyncio.create_task(
    session._run_one_skill(run_id, skill_name, input_artifact, chain_id=chain_id)
)
session.running_skills[run_id] = task

return {
    "status": "spawned",
    "run_id": run_id,
    "chain_id": chain_id,   # ← ルーター LLM が完了時に紐付けるために使う
    "note": "バックグラウンドで実行中。完了したらお知らせします。",
}
```

`dispatch_kind` は `"sync"` のままとする。ルーターループは tool result を inline で受け取り、
ルーター LLM を最後にもう 1 ターン呼び出す。LLM は
`{status: "spawned", chain_id: "abc123"}` を見てユーザー向けの確認応答を生成する:

```
Router → ユーザー:
  「skill_builder を起動しました（chain_id: abc123）。完了したらお知らせします。
   /tasks status で進捗を確認できます。」
```

バックグラウンドタスクはすでに実行中。セッションループはルーターが応答し次第、
次の inbox メッセージを即座に処理できる状態になる。

**ルーターシステムプロンプト追記:**

```
- invoke_skill が {status: "spawned", chain_id: ...} を返したら:
  何を起動したか・chain_id・完了時に通知することをユーザーに伝える。
  /tasks status で進捗確認できることを案内する。
  タスクが完了するまで追加の質問をしない。
```

### Phase 2 — user message 注入による完了通知（narrator なし）

#### なぜ tool_result が使えないか

LLM API（OpenAI Chat Completions・Anthropic Messages API ともに）は厳格な制約を設けている:
`tool_result` / `role: "tool"` メッセージは、対応する `tool_use` / `tool_calls` ブロックを
含む直前の `assistant` メッセージがなければ API が 400 エラーを返す。
非同期タスクが完了した時点では、その `invoke_skill` の `tool_use` にはすでに
`{status: "spawned"}` という tool_result が対応付いており、追加の tool_result を
注入できる open な `tool_use` は存在しない。

これが主要マルチエージェントフレームワークが非同期完了を tool_result として LLM に
届けられない根本原因である。各フレームワークは完了まで実質ブロックするか、
コンテキストなしで新規 LLM ターンを起こすかのどちらかで対処している。

#### 正しいアプローチ: chain_id 付き user message 注入

`_run_one_skill` 完了時、`_invoke_narrator` を呼ぶ代わりに
`"skill_completed"` メッセージを inbox にエンキューする:

```python
# _run_one_skill — 完了時
await self._put_inbox("skill_completed", {
    "run_id": run_id,
    "skill": skill_name,
    "status": result.status,
    "data": result.data,
    "chain_id": chain_id,
})
```

`session.run()` ループが他のメッセージと同様にこれを処理する:

```python
elif kind == "skill_completed":
    await self._handle_skill_completed(payload)
```

`_handle_skill_completed` は `chain_id` と結果を持つ `user` ロールメッセージを
**既存の会話スレッドに注入**し、ルーター LLM を 1 ターン実行する:

```python
# セッションのメッセージ履歴に注入（role="user"）
"[task_completed] chain_id=abc123\n"
"skill: skill_builder  status: finished\n"
"result: {\"skill_name\": \"my_skill\", \"path\": \"reyn/project/my_skill/skill.md\"}\n\n"
"完了内容を 1〜2 文でユーザーに伝えてください。"
```

ルーター LLM が narration を生成 → ユーザー outbox へ push。

**なぜ system addendum（新規 LLM ターン）ではなく user message 注入か？**

既存スレッドへの注入によって、ルーター LLM は完全な会話コンテキストを持てる:
最初の `invoke_skill` 呼び出し・`{status: "spawned", chain_id: "abc123"}` という
tool result・その後のユーザーとのやり取り・そして今回の完了通知、すべてが
1 本のスレッドで見える。`chain_id` がスポーン時の呼び出しと完了通知を明確に
紐付けるため、複数スキルが並行動作している場合も LLM はどのタスクが完了したかを
正確に把握できる。新規 LLM ターン（system addendum）ではこのコンテキストが
すべて失われ、narration の品質が低下する。

### Phase 3 — スラッシュコマンド強化

既存コマンド（`/skill list`、`/skill discard`）は維持・強化。
スキル・プラン・エージェント委任を横断する統一エントリーポイント `/tasks` を追加:

```
/tasks                          → 全実行中タスク一覧（スキル + プラン + 委任）
/tasks kill <run_id_prefix>     → 指定タスクをキャンセル（/skill discard のラッパー）
/tasks status <run_id_prefix>   → 現在フェーズ・経過時間・最新 P6 イベントを表示
```

**`/tasks status` 出力例:**

```
skill_builder [abc1]  実行中  2分14秒
  フェーズ:  apply_improvements（3/5 イテレーション）
  最終 op:  write_file reyn/project/my_skill/phases/plan.md
  コスト:   $0.08
```

`running_skills`・`running_skills_started_at`・P6 イベントログから読み取る。
新規セッション状態は不要。

---

## 変更後のメッセージフロー

```
ユーザー: "skill_builder を動かして"
  └─ RouterLoop: invoke_skill(name="skill_builder")
       └─ _handle: create_task(...) → {status:"spawned", chain_id:"abc123"} を即返却
       └─ ルーター LLM が tool_result を inline で確認、確認応答を生成
  └─ Router LLM → ユーザー: "skill_builder を起動しました (chain_id: abc123)。/tasks で進捗を確認できます。"
  └─ セッションループ: 解放 — 次のメッセージを即座に処理

ユーザー: "ちなみに recall の設定どうなってた？"
  └─ RouterLoop: recall(...) → router LLM がインラインで回答
  └─ ユーザーが回答を受け取る — スキルはバックグラウンドで実行中

[2 分後] skill_builder 完了
  └─ inbox: ("skill_completed", {skill:"skill_builder", chain_id:"abc123", status:"finished", data:{...}})
  └─ _handle_skill_completed:
       └─ 既存会話スレッドに user message を注入:
            "[task_completed] chain_id=abc123 / skill: skill_builder / status: finished
             result: {skill_name: my_skill, path: reyn/project/my_skill/skill.md}
             完了内容を 1〜2 文でユーザーに伝えてください。"
       └─ ルーター LLM を 1 ターン実行（スポーン〜完了の全コンテキスト参照可能）
  └─ Router LLM → ユーザー: "skill_builder が完了しました。reyn/project/my_skill/ に作成されました。"
```

---

## Proposed implementation

### Component A — `invoke_skill` ノンブロッキング spawn 化（MEDIUM）

- **`dispatch_kind` は変更しない**（`"sync"` のまま）
- `_handle` が `create_task` をスポーンし**即座に**
  `{"status": "spawned", "run_id": ..., "chain_id": ..., "note": ...}` を返却
- ルーター LLM はこの tool result を inline で受け取り同ターンで確認応答を生成
- `_run_skill_awaitable` を削除（または内部ユーティリティとして保持）
- `_run_one_skill` 完了時に `"skill_completed"` を inbox にエンキュー

### Component B — `session.run()` ループに `"skill_completed"` を追加（SMALL）

- `elif kind == "skill_completed"` ブランチを追加
- `_handle_skill_completed` が `chain_id` + 結果を持つ `user` メッセージを
  **既存会話スレッドに注入**し、ルーター LLM を 1 ターン実行
- ルーターは全会話コンテキスト（スポーン〜完了）を参照して narration を生成 → outbox へ

### Component C — ルーターシステムプロンプト更新（SMALL）

- スポーン後の確認応答ガイダンス: `{status: "spawned", chain_id: ...}` を受け取ったら
  起動内容・chain_id・`/tasks status` を案内（Phase 1）
- 完了後の narration ガイダンス: `[task_completed]` user message を受け取ったら
  1〜2 文でステータス別に narrate（Phase 2）

### Component D — `/tasks` スラッシュコマンド（SMALL）

- `slash/tasks.py` 新規作成（list / kill / status サブコマンド）
- 既存の `running_skills` + `running_skills_started_at` + P6 イベントログを読み取る
- `/skill discard`・`/plan discard` はエイリアスとして維持

### Component E — `_run_skill_awaitable` とデッドコードの削除（SMALL）

- `_dispatch_routing_decision_for_user` を削除（呼び出し元ゼロの確認済みデッドコード）
- `_run_skill_awaitable` を削除またはルーターパス以外に限定
- FP-0011 Component A（`_run_skill_awaitable` からの narrator 呼び出し削除）を包含

---

## FP-0011 との関係

| 懸念事項 | FP-0011 | FP-0012 |
|---|---|---|
| `_run_skill_awaitable` からの narrator 呼び出し削除 | Component A | Component E（包含） |
| ルーター LLM がスキル完了を narrate | Component B | Component B・Phase 2（包含） |
| `skill_narrator` スキル削除 | Component C | 対象外 — 独立して先行可 |
| narrator テスト削除 | Component D | 対象外 — 独立して先行可 |
| ノンブロッキング実行 | 対処なし | **中心目標** |
| inbox 経由の完了再エントリー | 対処なし | Component B |
| `/tasks` スラッシュコマンド | 対処なし | Component D |

**推奨**: FP-0011 の Component C/D（スキル削除・テスト削除）を先行クリーンアップとして
先にランディングし、FP-0012 の Component A–E で非同期実行モデル全体を実装する。

---

## Dependencies

- `asyncio.create_task` + `running_skills` dict — session.py にすでに存在
- `chain_id` — session.py にすでに存在（`running_skills_chain`）
- P6 イベントログ — `/tasks status` で使用（新規イベント追加不要）
- FP-0011 — 一部重複あり、上記の関係表を参照

---

## Cost estimate

**合計: LARGE**

| タスク | コスト | 備考 |
|---|---|---|
| Component A: invoke_skill 非同期ディスパッチ化 | MEDIUM | コア変更。invoke_skill.py + session.py |
| Component B: skill_completed inbox + ハンドラー | SMALL | 新規 inbox kind + ルーター 1 ターン |
| Component C: ルーター SP 更新 | SMALL | 約 10 行追加 |
| Component D: `/tasks` スラッシュコマンド | SMALL | slash/tasks.py 新規作成 |
| Component E: デッドコード削除 | SMALL | `_dispatch_routing_decision_for_user` 削除 |
| テスト | MEDIUM | Tier 2: ループノンブロッキング契約 / 完了再エントリー |

リスク: 弱いモデルでのスポーン確認応答・完了 narration の品質。
Component C の SP ガイダンスで軽減するが、ランディング前に G4 spike 推奨。

---

## Related

- `src/reyn/chat/session.py` — `_run_skill_awaitable`、`_dispatch_routing_decision_for_user`、`run()` ループ
- `src/reyn/tools/invoke_skill.py` — `INVOKE_SKILL` dispatch_kind
- `src/reyn/chat/slash/skill.py` — 既存 `/skill list` と `/skill discard`
- `src/reyn/chat/router_system_prompt.py` — Component C の挿入箇所
- FP-0011 (`0011-remove-narrator.md`) — narrator 廃止。本 FP に部分的に包含される
