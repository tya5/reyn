# FP-0001: A2A task lifecycle — ask_user / push notification 対応

**Status**: proposed
**Proposed**: 2026-05-09
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

現在の A2A 実装は `message/send`（同期）のみで、スキル実行中に `ask_user` が発生すると
タイムアウト応答しか返せない。`RunRegistry` を中心とする task lifecycle を実装することで、
`ask_user` の中断・再開、push notification、SSE ストリーミングを一括対応できる。

---

## Motivation

### 現状の制約

```
Client ──POST /a2a/{name}──▶ message/send 開始
                              skill 実行中...
                              ask_user 発火 ← ここで止まる
◀── timeout / partial ──────  answer を注入する経路がない
```

- `ask_user` を含む skill が A2A 経由では実質使えない
- クライアントが進捗を知る手段がない（ポーリング不可）
- Agent Card に `streaming: false`, `pushNotifications: false` と宣言しており、
  競合（Hermes Agent の Checkpoints v2 等）に対して機能差が明確

### ACP との関係

ACP（IBM BeeAI）は A2A の傘下に統合済み（2026-05 時点）。
ACP の `await_resume` モデルは本提案の `ask_user` 対応と完全に対応する。
本実装を行えば A2A と ACP の両プロトコルを同一基盤で賄える。

---

## Proposed implementation

### 中心: RunRegistry

```python
# src/reyn/web/run_registry.py（新規）
{
  run_id: {
    "task":        asyncio.Task,          # バックグラウンドで走る skill
    "status":      "running" | "input-required" | "completed" | "failed",
    "question":    str | None,            # ask_user の質問文
    "intervention": UserIntervention | None,  # answer 待ち Future
    "result":      str | None,
    "webhook_url": str | None,            # push notification 宛先
  }
}
```

### フロー（ask_user）

```
1. POST /a2a/{name}  →  run_id 発行、asyncio.create_task() で skill 起動
2. ask_user 発火     →  status = "input-required", question を RunRegistry に格納
3. GET tasks/{run_id} → {status: "input-required", question: "..."}
4. POST /a2a/{name} {task_id, answer}  →  InterventionBus.answer(text)
5. skill 再開 → status = "completed", result 格納
```

### 追加エンドポイント

| エンドポイント | 用途 |
|---|---|
| `GET /a2a/tasks/{run_id}` | task ステータス・質問文のポーリング |
| `GET /a2a/tasks/{run_id}/events` | SSE ストリーム（EventLog を run_id フィルタ） |
| `POST /a2a/tasks/{run_id}/cancel` | task キャンセル |

`message/send` は `task_id` パラメータを追加し、既存タスクへの answer 注入と新規タスク開始を兼務。

### push notification

```python
async def _notify(run_id: str, status: str, payload: dict):
    reg = run_registry[run_id]
    reg["status"] = status
    if url := reg.get("webhook_url"):
        async with httpx.AsyncClient() as c:
            await c.post(url, json={"run_id": run_id, "status": status, **payload})
```

トリガーポイント: skill 開始 / ask_user 発火 / skill 完了 / エラー の 4 箇所のみ。

### Agent Card の更新

実装完了後に以下を `true` に更新:

```python
"capabilities": {
    "streaming": True,           # SSE 対応
    "pushNotifications": True,   # webhook 対応
    "stateTransitionHistory": False,  # 引き続き未対応
}
```

---

## Dependencies

- `src/reyn/web/routers/a2a.py`（既存 — 変更対象）
- `src/reyn/user_intervention.py` / `InterventionBus`（既存 — ブリッジを追加）
- `httpx`（FastAPI プロジェクトに既存の可能性が高い。なければ追加）

前提 PR: なし（独立して実装可能）

---

## Cost estimate

**合計: MEDIUM**

| タスク | コスト | 備考 |
|---|---|---|
| `RunRegistry` 実装 | SMALL | in-memory dict + asyncio.Task 管理 |
| `message/send` をバックグラウンド化 | SMALL | `create_task` に切り替えるだけ |
| `tasks/get` エンドポイント | SMALL | Registry 読み取りのみ |
| `InterventionBus` ブリッジ | MEDIUM | ask_user 発火時に Registry を更新するフック |
| Push notification | SMALL | httpx.post 1 箇所 |
| SSE streaming | SMALL | FastAPI StreamingResponse + EventLog.subscribe |
| `tasks/cancel` | SMALL | asyncio.Task.cancel() |
| Agent Card 更新 | SMALL | capabilities フラグ変更 |

ボトルネックは **InterventionBus ブリッジのみ**。それ以外は全て SMALL で連鎖する。

---

## Related

- `src/reyn/web/routers/a2a.py` — 既存 A2A 実装（MVP コメント参照）
- `src/reyn/user_intervention.py` — InterventionBus の実装
- `docs/concepts/multi-agent/a2a.md` — A2A 概念ドキュメント
- ACP OpenAPI spec: https://github.com/i-am-bee/acp/blob/main/docs/spec/openapi.yaml
