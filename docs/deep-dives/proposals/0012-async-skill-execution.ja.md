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

`invoke_skill` を fire-and-forget（非同期ディスパッチ）に変更し、スポーン直後に
ルーター LLM へ結果を即返却し、タスク完了時は narrator を挟まずにルーターへ
再エントリーして結果を narrate する設計に刷新する。

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

### Phase 1 — invoke_skill を非同期ディスパッチへ

**`invoke_skill` はスポーン直後に即 return する:**

```python
# invoke_skill.py の _handle — バリデーション後
task = asyncio.create_task(
    session._run_one_skill(run_id, skill_name, input_artifact, chain_id=chain_id)
)
session.running_skills[run_id] = task

return {
    "status": "spawned",
    "run_id": run_id,
    "note": "バックグラウンドで実行中。完了したらお知らせします。",
}
```

`invoke_skill` を `dispatch_kind="async"` で登録することで、ルーターループは
`delegate_to_agent` と同じブランチで即 return する。ルーター LLM はツール結果を
インラインで受け取らず、終了を検知してユーザー向けの確認応答を生成する:

```
Router → ユーザー:
  「skill_builder を起動しました。完了したらお知らせします。
   /skill list で進捗を確認できます。」
```

セッションループは即座に次の inbox メッセージを処理できる状態になる。

**ルーターシステムプロンプト追記:**

```
- invoke_skill がタスクをスポーンしたら: 何を起動したかとタスク完了時に通知することをユーザーに伝える。
  進捗確認に /skill list を案内する。タスクが完了するまで追加の質問をしない。
```

### Phase 2 — 完了時のルーター再エントリー（narrator なし）

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

`_handle_skill_completed` は結果をコンテキストとして注入して
ルーター LLM を 1 ターン実行する:

```
[system addendum]:
  非同期で起動したタスクが完了しました。
  skill: skill_builder
  status: finished
  result: {"skill_name": "my_skill", "path": "reyn/project/my_skill/skill.md"}

  完了内容を 1〜2 文でユーザーに伝えてください。
  ステータス別ガイダンス（FP-0011 Component B と同内容）。
```

ルーター LLM が narration を生成 → ユーザー outbox へ push。
これにより narrator が不要になり、FP-0011 の完了 narration パスを完全に置き換える。

**なぜ narrator ではなくルーター再エントリーか？**

FP-0011 と同じ理由: ルーター LLM はすでに他のすべてのツール結果（recall、list_skills など）を
インラインで narrate している。スキル完了は構造的に同一の「narrate すべきツール結果」。
narration パスを 1 本（ルーター LLM）に統一することがより整合性が高い。

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
  └─ RouterLoop: invoke_skill(name="skill_builder") → タスクをスポーン、即 return
  └─ Router LLM → ユーザー: "skill_builder を起動しました。/tasks で進捗を確認できます。"
  └─ セッションループ: 解放 — 次のメッセージを即座に処理

ユーザー: "ちなみに recall の設定どうなってた？"
  └─ RouterLoop: recall(...) → router LLM がインラインで回答
  └─ ユーザーが回答を受け取る — スキルはバックグラウンドで実行中

[2 分後] skill_builder 完了
  └─ inbox: ("skill_completed", {skill: "skill_builder", status: "finished", data: {...}})
  └─ _handle_skill_completed → 結果コンテキスト付きでルーター LLM 1 ターン実行
  └─ Router LLM → ユーザー: "skill_builder が完了しました。reyn/project/my_skill/ に作成されました。"
```

---

## Proposed implementation

### Component A — `invoke_skill` 非同期ディスパッチ化（MEDIUM）

- `INVOKE_SKILL` を `dispatch_kind="async"` に変更
- `_handle` で `create_task` をスポーンし `{"status": "spawned", "run_id": ..., "note": ...}` を即返却
- `_run_skill_awaitable` を削除（または内部ユーティリティとして保持）
- `_run_one_skill` 完了時に `"skill_completed"` を inbox にエンキュー

### Component B — `session.run()` ループに `"skill_completed"` を追加（SMALL）

- `elif kind == "skill_completed"` ブランチを追加
- `_handle_skill_completed` でコンパクトなルーターコンテキストを構築し 1 ターン実行
- ルーターが narration を生成 → outbox へ

### Component C — ルーターシステムプロンプト更新（SMALL）

- スポーン後の確認応答ガイダンス（Phase 1）
- 完了後の narration ガイダンス（Phase 2、FP-0011 Component B と同内容）

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
- `dispatch_kind="async"` パターン — `delegate_to_agent` での使用例あり
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
