# FP-0021: イベントログ監査完全性 — 欠落している run_id とアクターコンテキストの追加

**Status**: **done** — LANDED 2026-05-13、 6-commit chain `c6f4218`..`a03bcfc`: OpContext.run_id field + 7 events `run_id`/`skill` 追加 + 新 `permission_granted` event + `intervention_id` 相関 + EVENT_AUDIT_REQUIREMENTS registry + Tier 2 invariant test
**Proposed**: 2026-05-13
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

`workflow_started` は `run_id` と `skill` を正しく持っているが、同一 run 中に発行される
6 種類のイベントタイプはどちらも持っていない。これにより `events/*.jsonl` 監査ログは、
`workflow_started` のタイムスタンプ近傍で結合しなければ run 単位で集計できない——並行
run が存在する環境では壊れる。本提案は欠落している emit 呼び出しに `run_id`・`skill` を
追加し、パーミッション発行イベントに `phase` を追加し、現在は何も発行されていない
allow パスに `permission_granted` イベントを新設する。

---

## Motivation

### ギャップの全体像

`workflow_started` だけが常に `run_id` と `skill` を持つ：

```python
self.events.emit("workflow_started",
    run_id=self.run_id, skill=self.skill.name, ...)
```

同一 run から発行される以下のイベントはどちらも欠落している：

| イベント | 現在のフィールド | 不足 |
|---|---|---|
| `workflow_finished` | `phase`, `reason`, `confidence`, `total_phase_count`, `final_output_keys` | `run_id`, `skill` |
| `llm_called` | `phase`, `model` | `run_id`, `skill` |
| `llm_response_received` | `phase`, `response_type`, `raw`, tokens, `cost_usd` | `run_id`, `skill` |
| `permission_denied` | `kind`, `path`, `reason` | `run_id`, `skill`, `phase` |
| `user_intervention_requested` | `phase`, `question`, `suggestions` | `run_id`, `skill` |
| `user_intervention_received` | `phase`, `answer` | `run_id`, `skill`、`requested` との紐付けなし |

さらに：**`permission_granted` イベントが存在しない**。allow パスは何も発行しないため、
監査ログは拒否だけを記録し承認は記録しない——非対称な監査証跡になっている。

### 監査上の問題

「スキル X が run Y で行った LLM 呼び出しをすべて示せ」という監査要求は現状：
1. run Y の `workflow_started` からタイムスタンプ範囲を取得
2. そのウィンドウ内の `llm_called` を集める
3. 他の run との重複がなかったことを祈る

`run_id` が全イベントに付けばステップ 1〜3 が `run_id == Y` の単一フィルタに集約される。

`docs/concepts/events.md` の設計ドキュメントはすでに `run_id` を安定エンベロープフィールド
として記述している。本 FP は設計書と実装のギャップを埋める。

### クラッシュ復元との切り離し

これは純粋な可観測性の変更。WAL（`state_log.jsonl`）はクラッシュ復元を独立して処理しており、
これらの emit 呼び出しは WAL に一切触れない。`emit()` への kwarg 追加は復元の正確性に
ゼロリスク。

---

## Proposed implementation

### 7 箇所の変更（すべて既存の呼び出しサイト）

**1. `workflow_finished`** — `src/reyn/kernel/runtime.py`

```python
# 変更前
self.events.emit("workflow_finished",
    phase=phase, reason=reason, ...)

# 変更後
self.events.emit("workflow_finished",
    run_id=self.run_id, skill=self.skill.name,
    phase=phase, reason=reason, ...)
```

**2. `llm_called`** — `src/reyn/kernel/runtime.py`

```python
# 変更前
self.events.emit("llm_called", phase=phase, model=resolved_model)

# 変更後
self.events.emit("llm_called",
    run_id=self.run_id, skill=self.skill.name,
    phase=phase, model=resolved_model)
```

**3. `llm_response_received`** — `src/reyn/kernel/runtime.py`

```python
# 変更前
self.events.emit("llm_response_received", phase=phase, ...)

# 変更後
self.events.emit("llm_response_received",
    run_id=self.run_id, skill=self.skill.name,
    phase=phase, ...)
```

**4. `permission_denied`** — `src/reyn/op_runtime/__init__.py`

```python
# 変更前
ctx.events.emit("permission_denied", kind=op.kind, path=path, reason=str(exc))

# 変更後
ctx.events.emit("permission_denied",
    run_id=ctx.run_id, skill=ctx.skill_name, phase=ctx.current_phase,
    kind=op.kind, path=path, reason=str(exc))
```

注意: `OpContext` に `run_id`・`skill_name` フィールドがなければ追加が必要。
`src/reyn/op_runtime/context.py` を確認すること。

**5. `user_intervention_requested`** — `src/reyn/op_runtime/ask_user.py`

```python
# 変更前
ctx.events.emit("user_intervention_requested",
    phase=ctx.current_phase, question=op.question, suggestions=...)

# 変更後
ctx.events.emit("user_intervention_requested",
    run_id=ctx.run_id, skill=ctx.skill_name,
    phase=ctx.current_phase, question=op.question,
    intervention_id=iv.id,     # ← received との紐付けに使用
    suggestions=...)
```

**6. `user_intervention_received`** — `src/reyn/op_runtime/ask_user.py`

```python
# 変更前
ctx.events.emit("user_intervention_received", phase=ctx.current_phase, answer=text)

# 変更後
ctx.events.emit("user_intervention_received",
    run_id=ctx.run_id, skill=ctx.skill_name,
    phase=ctx.current_phase, answer=text,
    intervention_id=iv.id)     # ← requested に対応
```

**7. `permission_granted`（新規イベント）** — `src/reyn/op_runtime/__init__.py` または
`src/reyn/op_runtime/dispatcher.py`

パーミッションチェック通過後、`permission_denied` と対称に発行：

```python
ctx.events.emit("permission_granted",
    run_id=ctx.run_id, skill=ctx.skill_name, phase=ctx.current_phase,
    kind=op.kind, path=path)
```

---

## 対象ファイル

| ファイル | 変更内容 |
|---|---|
| `src/reyn/kernel/runtime.py` | `workflow_finished`・`llm_called`・`llm_response_received` に `run_id`・`skill` を追加 |
| `src/reyn/op_runtime/__init__.py` | `permission_denied` に `run_id`・`skill`・`phase` を追加；`permission_granted` を新設 |
| `src/reyn/op_runtime/ask_user.py` | 両インターベンションイベントに `run_id`・`skill`・`intervention_id` を追加 |
| `src/reyn/op_runtime/context.py` | 未保持の場合 `OpContext` に `run_id`・`skill_name` を追加 |
| `docs/concepts/events.md` | `kind` → `type` に修正；`run_id` が一貫して存在する旨を反映 |

---

## Dependencies

なし。すべての呼び出しサイトは `run_id` と `skill` にすでにアクセスできる（`OSRuntime`
の `self`、op_runtime の `ctx` 経由）。構造的な変更は不要。

---

## Cost estimate

| タスク | コスト |
|---|---|
| runtime の 3 イベントに `run_id`/`skill` を追加 | SMALL |
| `permission_denied` に `run_id`/`skill`/`phase` を追加 | SMALL |
| `permission_granted` イベントを新設 | SMALL |
| インターベンションイベントに `run_id`/`skill`/`intervention_id` を追加 | SMALL |
| `docs/concepts/events.md` 更新 | SMALL |
| **合計** | **SMALL** |

すべて加算的な kwarg 追加——既存のコンシューマは未知のフィールドを無視するため壊れない。

---

## Related

- `src/reyn/kernel/runtime.py` — `workflow_finished`・`llm_called`・`llm_response_received`
- `src/reyn/op_runtime/__init__.py` — `permission_denied`
- `src/reyn/op_runtime/ask_user.py` — `user_intervention_requested/received`
- `docs/concepts/events.md` — `run_id` を安定エンベロープフィールドとして記述する設計ドキュメント
- FP-0018 (`0018-event-store-backend.md`) — 将来の SQLite/DuckDB バックエンドでこの豊富なイベントが直接クエリ可能になる
- FP-0007 (`0007-evaluation-infrastructure.md`) — eval トレース export が `run_id` 相関付きの `llm_called` から直接利益を得る
