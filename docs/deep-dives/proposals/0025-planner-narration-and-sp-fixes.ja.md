# FP-0025: Planner — Router Narration + Plan Step SP 修正

**Status**: proposed
**Proposed**: 2026-05-14
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

plan 完了時の narration を FP-0012 で確立したスキル narration パターンと同形にする。terminal plan ステップがテキストを直接ユーザーに流す現在の設計を、`plan_completed` inbox メッセージをエンキューして router LLM が 1 ターン narrate する設計に変更。あわせて `build_plan_step_system_prompt` の 3 つの独立した問題（`output_language` 未伝達・step id 露出・Router SP の plan 使用基準欠如）を修正する。

---

## Motivation

### 現在の plan 完了フロー

```
plan ツール呼び出し
  → dispatch_plan_tool → {"status": "spawned"}
  → router LLM が spawn-ack を返す（1 文）

[バックグラウンド]
  → PlanRuntime.run() → execute_plan()
  → terminal ステップの LLM がユーザー向けテキストを生成  ← synthesis 負荷
  → spawn_plan_task: _put_outbox(kind="agent", text=result_text)  ← 直接ユーザーへ
```

terminal ステップの LLM が synthesis の全責任を負い、router を経由せずにユーザーへ届く。router は plan 結果を見ないため、元のクエリとの整合性チェックも言語設定の適用もできない。

### スキル narration（FP-0012 着地済み）

```
invoke_skill 呼び出し
  → {"status": "spawned"}
  → router LLM が spawn-ack を返す

[バックグラウンド]
  → OSRuntime.run()
  → _enqueue_skill_completed → inbox "skill_completed"
  → session.run() ループ: _handle_skill_completed
      [task_completed] user-role メッセージを history に inject
      router LLM が 1 ターン実行
  → router LLM が narrate → ユーザーへ
```

plan フローはスキルと非対称。同形にすることで router が synthesis・言語修正・品質チェックを担える。

### `build_plan_step_system_prompt` の追加問題

**問題 1 — `output_language` が伝わらない**
`_PlanStepHost.output_language` は親 host から引き継いでいるが、`build_plan_step_system_prompt` に `output_language` パラメータがない。JA ユーザーが plan mode を使うとステップ返答が EN になりやすい。

**問題 2 — step id がプロンプトに露出**
```python
parts.append(f"## This step (id={step.id})\n{step.description}")
```
内部識別子（`"s1"`, `"s2"` 等）が LLM コンテキストに現れ、ステップ返答に漏れると `prior_results` 経由で narration ターンまで伝播するリスクがある。

**問題 3 — Router SP Behaviour に plan 使用基準がない**
`plan` ツールはスキーマの description だけで「いつ使うか」を LLM に判断させている。`invoke_skill` や `delegate_to_agent` と異なり、Router SP の Behaviour セクションに補強ルールがない。

---

## Proposed implementation

### Component A — `output_language` 引き渡し（SMALL）

**`src/reyn/chat/planner.py`** — `build_plan_step_system_prompt` シグネチャ追加:

```python
def build_plan_step_system_prompt(
    plan: Plan,
    step: PlanStep,
    prior_results: dict[str, str],
    *,
    output_language: str | None = None,   # 追加
) -> str:
    parts: list[str] = []
    if output_language:
        parts.append(f"Respond in {output_language}.")
        parts.append("")
    parts.append("You are a Reyn agent ...")
    ...
```

呼び出し箇所（`execute_plan` 内）:

```python
sys_prompt = build_plan_step_system_prompt(
    plan, step, step_results,
    output_language=narrow_host.output_language,
)
```

### Component B — step id 除去（SMALL）

**`src/reyn/chat/planner.py`** — ステップヘッダー変更:

```python
# 変更前
parts.append(f"## This step (id={step.id})\n{step.description}")

# 変更後
parts.append(f"## Your task\n{step.description}")
```

step id は P6 イベントと検証エラーで参照できるため、LLM コンテキストへの露出は不要。

Component C（Router narration）と合わせて、出力ガイダンスも更新:

```python
# 変更前
"emit a concise text reply (100-400 chars) summarising what "
"this step contributes to the plan goal."

# 変更後
"Summarise what this step found in 1–3 sentences. "
"Be factual; a separate synthesis step will produce the user reply."
```

Router が synthesis を担うため、各ステップは情報収集に集中すれば良い。

### Component C — Router narration（= スキルと同形、SMALL）

FP-0012 パターンを plan に適用する。

#### C.1 — `_enqueue_plan_completed`（新規、session.py）

```python
async def _enqueue_plan_completed(
    self,
    *,
    plan_id: str,
    chain_id: str,
    goal: str,
    step_results: dict[str, str],
    n_steps: int,
) -> None:
    """FP-0025: plan_completed inbox メッセージをエンキューして router narration を起動。"""
    try:
        await self._put_inbox(
            "plan_completed",
            {
                "plan_id": plan_id,
                "chain_id": chain_id,
                "goal": goal,
                "step_results": step_results,
                "n_steps": n_steps,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("_enqueue_plan_completed failed for %s: %r", plan_id, exc)
```

#### C.2 — `_handle_plan_completed`（新規、session.py）

```python
async def _handle_plan_completed(self, payload: dict) -> None:
    """FP-0025: _handle_skill_completed（FP-0012）と対称の plan narration ハンドラ。"""
    plan_id = payload.get("plan_id", "")
    chain_id = payload.get("chain_id") or _new_chain_id()
    goal = payload.get("goal", "")
    step_results = payload.get("step_results") or {}
    try:
        results_str = json.dumps(step_results, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        results_str = repr(step_results)
    injected_text = (
        f"[plan_completed] plan_id={plan_id}\n"
        f"goal: {goal}\n"
        f"step_results:\n{results_str}\n\n"
        "Please synthesize the step results into a complete response for the user."
    )
    self._append_history(ChatMessage(
        role="user", text=injected_text, ts=_now_iso(),
        meta={
            "source": "plan_completion",
            "plan_id": plan_id,
            "chain_id": chain_id,
        },
    ))
    self._chat_events.emit(
        "plan_completion_injected",
        plan_id=plan_id, chain_id=chain_id,
    )
    await self._run_router_turn(chain_id=chain_id)
```

#### C.3 — `spawn_plan_task` 変更（session.py）

```python
# 変更前
if clean_exit and result_text:
    await self._put_outbox(OutboxMessage(
        kind="agent", text=result_text,
        meta={"plan_id": plan_id, "source": "plan", ...},
    ))

# 変更後
if clean_exit and result is not None:
    await self._enqueue_plan_completed(
        plan_id=plan_id, chain_id=chain_id,
        goal=result.plan_goal,   # PlanExecutionResult に plan_goal + n_steps を追加
        step_results=result.step_results,
        n_steps=result.n_steps,
    )
```

#### C.4 — Session run() ループへの登録（session.py）

```python
elif kind == "plan_completed":
    await self._handle_plan_completed(payload)
```

#### C.5 — plan tool description 更新

`src/reyn/tools/plan.py` および `src/reyn/chat/router_tools.py` フォールバックリテラル:

```python
# 変更前
"The terminal step's text reply becomes the user-facing answer; "
"design the last step to synthesise."

# 変更後
"Each step summarises what it found; the router synthesises the "
"final reply after all steps complete."
```

`steps_json` の `"Use [] for steps that just synthesise"` ガイダンスも削除。全ステップが focused 情報収集になるため。

### Component D — Router SP Behaviour に plan 使用基準（SMALL）

**`src/reyn/chat/router_system_prompt.py`** — Behaviour セクションの `invoke_skill` / `delegate_to_agent` ルールの後に追加:

```markdown
## プランの分解

複数の独立したソースを横断して情報を組み合わせる必要がある場合に `plan` ツールを使う:
  - 「A と B をドキュメント間で比較して」
  - 「コード参照付きで X を説明して」
  - 「N ファイルにわたってまとめて」

使ってはいけない状況:
  - 単一ツール呼び出しや単一ソース narration
  - 会話的な返答
  - invoke_skill が end-to-end で処理できるタスク
  - ツールなしで 1 ターンで答えられるクエリ
```

---

## 対象ファイル

| ファイル | 変更内容 |
|---|---|
| `src/reyn/chat/planner.py` | A: `output_language` パラメータ; B: step id 除去・出力ガイダンス更新; C: plan step SP 更新 |
| `src/reyn/chat/session.py` | C: `_enqueue_plan_completed`・`_handle_plan_completed`・`spawn_plan_task` 変更・run() ループ |
| `src/reyn/chat/router_system_prompt.py` | D: plan 使用基準 Behaviour ルール |
| `src/reyn/tools/plan.py` | C: description 更新（terminal-step-as-synthesiser 削除） |
| `src/reyn/chat/router_tools.py` | C: フォールバックリテラル description 更新 |

---

## Dependencies

- Component C は FP-0012 着地済み（`_run_router_turn`・`_put_inbox`・`_append_history` パターンが存在）に依存。
- A・B・D は独立リリース可能。
- C は A/B と独立。

---

## Cost estimate

| コンポーネント | タスク | コスト |
|---|---|---|
| A | `output_language` パラメータ + 呼び出し箇所 | SMALL |
| B | step id 除去 + 出力ガイダンス更新 | SMALL |
| C | `_enqueue_plan_completed` + `_handle_plan_completed` + `spawn_plan_task` + ループ + description | SMALL |
| D | Router SP plan 使用基準ルール | SMALL |
| **合計** | | **SMALL** |

Component C は SMALL（MEDIUM ではない）。FP-0012 パターンがすでに確立・実証済みであり、設計の発明ではなく構造的な複製。

---

## Verification

1. **Component A**: `output_language: Japanese` でプラン実行 → 各ステップ返答が JA になることを確認。
2. **Component B**: `dogfood_trace --mode plan-trace` で step id（`s1`, `s2` 等）がステップのキャプチャテキストに現れないことを確認。
3. **Component C**: 3 ステップのプランクエリを実行して確認:
   - `plan_completion_injected` イベントが events ログに記録される
   - Router が synthesis された返答を生成（terminal ステップの生テキストではない）
   - history に `[plan_completed]` user-role メッセージが含まれる
   - `step_results` dict が inbox メッセージに含まれる
4. **Component D**: 単一ツールクエリで router が `plan` を呼ばないことを確認。複数ソース合成クエリで `plan` を呼ぶことを確認。

---

## Related

- FP-0012 (`0012-async-skill-execution.md`) — このパターンを複製するスキル narration（着地済み、commit `c9e79d6`）
- FP-0011 (`0011-remove-narrator.md`) — Router narration 方針
- FP-0023 (`0023-router-sp-quick-wins.md`) — 同方向の Router SP 改善
- `src/reyn/chat/session.py` — `_handle_skill_completed`・`_enqueue_skill_completed`（参照実装）
- `src/reyn/chat/planner.py` — `build_plan_step_system_prompt`・`execute_plan`
