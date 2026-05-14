# FP-0027: Plan ステップ失敗の透明性向上

**Status**: proposed
**Proposed**: 2026-05-14
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

プランステップが失敗（サブループ中の例外）した場合、失敗情報は `PlanExecutionResult.step_failures` に記録されるが、最終回答を合成する router LLM には転送されない。合成ステップは `step_results` のみを受け取り、失敗した依存ステップには `"(no result)"` というサイレントな代替値が渡される。その結果、router LLM は情報欠損を認識できないまま自信満々な回答を生成してしまう。本 FP は `step_failures` を `_handle_plan_completed` まで通し、router LLM がデータ欠損を把握して回答に反映できるようにする。

---

## Motivation

### 現在のサイレント劣化

```
step s1: auth.py を読む          → result: "JWT decode は lines 78-95"
step s2: session.py を読む       → 例外で失敗
step s3: depends_on [s1, s2]     → prior_results.get("s2") == "(no result)"

_handle_plan_completed 注入:
  step_results: {"s1": "JWT decode...", "s3": "..."}
  # s2 の失敗は不可視 — router LLM は session.py データが欠けていることを知らない
```

router LLM は「完全な回答」のように見えるが、session.py の分析が丸ごと欠けている。ユーザーは回答が部分的なものだと知る手段がない。

### 期待される動作

router LLM が次のように伝えられるべき：「auth.py の JWT 認証ロジックは確認できましたが、session.py が読み取れませんでした — セッション管理の説明が不完全な可能性があります。」これには合成ターンがどのステップが失敗したかを知る必要がある。

---

## Proposed implementation

### 1. `_enqueue_plan_completed` に `step_failures` を追加（session.py）

```python
async def _enqueue_plan_completed(
    self,
    *,
    plan_id: str,
    chain_id: str,
    goal: str,
    step_results: dict[str, str],
    step_failures: dict[str, str],   # ← 追加
    n_steps: int,
) -> None:
    await self._put_inbox(
        "plan_completed",
        {
            "plan_id": plan_id,
            "chain_id": chain_id,
            "goal": goal,
            "step_results": step_results,
            "step_failures": step_failures,  # ← 追加
            "n_steps": n_steps,
        },
    )
```

### 2. `spawn_plan_task` で `result.step_failures` を渡す（session.py）

```python
await self._enqueue_plan_completed(
    plan_id=plan_id,
    chain_id=parent_chain_id or chain_id,
    goal=result.plan_goal,
    step_results=result.step_results,
    step_failures=result.step_failures,  # ← 追加
    n_steps=result.n_steps,
)
```

`PlanExecutionResult.step_failures: dict[str, str]` はすでに存在（step_id → エラー repr のマップ）。`planner.py` への変更は不要。

### 3. `_handle_plan_completed` の注入メッセージに失敗情報を含める（session.py）

```python
step_failures = payload.get("step_failures") or {}

injected_text = (
    f"[plan_completed] plan_id={plan_id}\n"
    f"goal: {goal}\n"
    f"step_results:\n{results_str}\n"
)
if step_failures:
    try:
        failures_str = json.dumps(
            {sid: err[:200] for sid, err in step_failures.items()},
            ensure_ascii=False, indent=2,
        )
    except (TypeError, ValueError):
        failures_str = repr(step_failures)
    injected_text += (
        f"\nstep_failures (データ取得に失敗したステップ):\n{failures_str}\n"
        "Note: 利用可能な step_results から合成し、"
        "失敗したステップによる情報欠損があれば回答に明示してください。\n"
    )
injected_text += "\nPlease synthesize the step results into a complete response for the user."
```

エラーメッセージは 200 chars にトランケートして、大きなトレースバックが router コンテキストに注入されるのを防ぐ。

---

## 対象ファイル

| ファイル | 変更内容 |
|---|---|
| `src/reyn/chat/session.py` | `_enqueue_plan_completed` シグネチャ; `spawn_plan_task` 呼び出し箇所; `_handle_plan_completed` 注入メッセージ |

---

## Dependencies

なし。`PlanExecutionResult.step_failures` は `execute_plan` によってすでに設定されている。

---

## Cost estimate

SMALL — `session.py` 内の 3 箇所の局所的な変更のみ。プロトコルやスキーマの変更なし。

---

## Verification

1. 1 ステップが例外を発生させるプランを実行 → router の回答に欠損の明示がある（「X を取得できなかった」など）
2. 全ステップが成功 → `step_failures` が空 → 注入メッセージは現在の挙動と変わらない
3. 両ケースで `plan_completion_injected` イベントが記録される

---

## Related

- `src/reyn/chat/planner.py` — `PlanExecutionResult.step_failures`（すでに設定済み）
- `src/reyn/chat/session.py` — `_enqueue_plan_completed`, `_handle_plan_completed`, `spawn_plan_task`
- FP-0025 (`0025-planner-narration-and-sp-fixes.ja.md`) — 本 FP が拡張する router narration パターンを導入
