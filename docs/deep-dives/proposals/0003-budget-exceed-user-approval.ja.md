# FP-0003: budget 超過時のユーザー許諾・再開フロー

**Status**: done (landed 2026-05-10、 commit `2ec46c0`)
**Proposed**: 2026-05-10
**Author**: Research session (eager-shaw-389d9d)
**Implemented**: 2026-05-10 — `CostLimitConfig.ask_on_exceed` + `extension_calls` フィールド + `BudgetTracker.extend_chain_calls()` + `ChatSession._ask_budget_extension()` ask_user dispatch + 8 Tier 2 invariants (`tests/test_budget_extend_chain.py`)。 後続: FP-0005 Phase 2 で shared `handle_limit_exceeded` helper に統合 (`dad53f4`)、 `cost.per_chain_skill_calls` キー自体は legacy 撤去 (`0b464ab`) で `safety.loop.skill_calls_per_chain` に移動。

---

## Summary

現在 `per_chain_skill_calls` / `per_chain_skill_tokens` の hard limit に達すると
スキル起動が即時拒否され、再開手段がない。
budget 超過時に `ask_user` 経由でユーザーへ許諾を求め、承認された場合に
チェーンの budget をリセットして spawn を再試行する仕組みを追加する。

---

## Motivation

### 現状の問題

```
hard_limit 到達 → スキル起動を即時拒否（return）
                → ユーザーへエラーメッセージ
                → その spawn インスタンスは失われる
                → 再開するにはユーザーが手動で /budget reset を実行し
                  同じリクエストをもう一度送り直す必要がある
```

- 長時間の multi-agent タスクでチェーン途中に budget 上限に達した場合、
  それまでの途中結果が捨てられる
- `/budget reset` はカウンタ全体をリセットするため、
  意図しない他チェーンへの影響を防げない
- ユーザーが「この作業は続けて良い」と判断できるのに、
  システムが強制終了してしまう

### ユースケース

- 大規模コード生成タスクで `skill_builder` が想定より多く呼ばれた場合に途中確認
- 調査タスクで web_search / read_local_files が上限に達した際に承認して継続
- hard limit を保険として設定しつつ、必要なら突破できる柔軟性を持たせたい

---

## Proposed implementation

### フロー

```
hard_limit 到達
    ↓
ask_user("skill 'X' が上限 N 回に達しました。追加で M 回まで継続しますか？ [yes/no]")
    ↓
/answer yes  →  reset_chain(chain_id) + spawn を再試行（+M 回 extension）
/answer no   →  現在と同じ即時拒否（エラーメッセージ）
タイムアウト →  /answer no と同扱い（デフォルト拒否）
```

### 実装箇所

**session.py（budget 超過パス）:**

```python
# 現在
if not check.allowed:
    self._emit_budget_exceeded(...)
    return  # 即時拒否

# 変更後（ask_on_exceed が有効な場合）
if not check.allowed:
    if self._should_ask_on_exceed(check):
        approved = await self._ask_budget_approval(chain_id, skill_name, check)
        if approved:
            self._budget.extend_chain(chain_id, skill_name, extension_calls=N)
            # spawn を再実行
        else:
            self._emit_budget_exceeded(...)
            return
    else:
        self._emit_budget_exceeded(...)
        return
```

**InterventionBus との接続:**

既存の `InterventionBus.ask(question)` を利用。
`ask_user` Control IR op と同一の pause / resume 基盤を流用する。

### 設定

```yaml
# reyn.yaml
cost:
  per_chain_skill_calls:
    hard_limit: 5
    warn_ratio: 0.8
    ask_on_exceed: true    # 追加フラグ（デフォルト: false = 現在の挙動を維持）
    extension_calls: 3     # 承認時に追加付与する回数
```

`ask_on_exceed: false`（デフォルト）では現在の挙動を完全に維持する。
opt-in 設計のため既存ユーザーへの影響なし。

### extension の設計

- `reset_chain()` ではなく `extend_chain(chain_id, skill, +N)` として
  カウンタを部分的に拡張するのみ（他スキル・他チェーンに影響しない）
- 承認ごとに `extension_calls` 分だけ上限を引き上げ
- 何度でも承認可能（都度 ask_user を発火）

---

## Dependencies

- `src/reyn/budget/budget.py` — `extend_chain()` メソッド追加
- `src/reyn/chat/session.py` — budget 超過パスに ask_user フック追加
- `src/reyn/user_intervention.py` / `InterventionBus` — 既存、変更不要
- `src/reyn/config.py` — `CostLimitConfig` に `ask_on_exceed`, `extension_calls` 追加

前提 PR: なし（独立して実装可能）

---

## Cost estimate

**合計: SMALL**

| タスク | コスト | 備考 |
|---|---|---|
| `CostLimitConfig` にフラグ追加 | SMALL | フィールド 2 つ追加のみ |
| `extend_chain()` メソッド実装 | SMALL | カウンタ上限の部分拡張 |
| session.py 超過パスに ask_user 追加 | SMALL | InterventionBus 呼び出し 1 箇所 |
| タイムアウト時デフォルト拒否 | SMALL | ask_user の timeout 引数で対応 |

ボトルネックなし。全タスク SMALL。

---

## Related

- `src/reyn/budget/budget.py` — BudgetTracker 実装
- `src/reyn/chat/session.py:2554` — 現行の budget 超過パス
- `src/reyn/user_intervention.py` — InterventionBus（ask_user 基盤）
- FP-0001 (`0001-a2a-task-lifecycle.md`) — 同じ InterventionBus を A2A に繋げる提案
