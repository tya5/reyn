# FP-0029: プランステップのイテレーション予算 — `_PLAN_STEP_MAX_ITERATIONS` の増加

**Status**: proposed
**Proposed**: 2026-05-14
**Author**: Research session (eager-shaw-389d9d)

---

## Summary

`planner.py` の `_PLAN_STEP_MAX_ITERATIONS = 3` は現実的なプランステップに対して厳しすぎる。`list_dir` → `read_file` → `write_file`（narrate）を必要とするステップはすでに 3 ターンでバジェットを使い切る。予期しない迂回（2 回目の read、バリデーション呼び出し）が発生するとステップがサイレントにアボートする。ルーターのデフォルトは 5 である。プランステップのバジェットを 5 に引き上げ、オプションで `reyn.yaml` でオーバーライドできるようにすることで、ルーターのデフォルトと一致し、現実的なマルチ op タスクを処理するための十分な余裕を与える。

---

## Motivation

### 現在の上限

```python
_PLAN_STEP_MAX_ITERATIONS = 3   # list_dir + read_file + narrate = exactly 3
```

現実的なプランステップ:
1. `list_dir` でターゲットファイルを探す
2. 特定されたファイルに `read_file`
3. 2 回目の `read_file` が必要（依存参照）← バジェット到達 → **アボート**

ステップは narration ターンに到達することなくサイレントにアボートする。`step_results` のエントリが `"(no result)"` になり、合成の品質が低下する。

### なぜ 5 か？

- `_MAX_ROUTER_ITERATIONS`（ルーターのデフォルト）と一致する。
- 最も一般的な現実的パターンにヘッドルームを提供する:
  - `list_dir` + `read_file` + `read_file` + narrate（4 ops）
  - `read_file` + `web_search` + `read_file` + narrate（4 ops）
  - バリデーションやリトライを含むエッジケース（5 ops）
- 5 を超えるとステップが暴走または混乱している可能性が高い；5 は安全な上限。

---

## Proposed implementation

### 1. 定数を引き上げる（planner.py）

```python
# 変更前
_PLAN_STEP_MAX_ITERATIONS = 3

# 変更後
_PLAN_STEP_MAX_ITERATIONS = 5
```

### 2. オプション: `reyn.yaml` でのオーバーライド

```yaml
# reyn.yaml
plan:
  step_max_iterations: 5   # デフォルト；プロジェクト単位でオーバーライド可能
```

```python
# planner.py — 設定がある場合はそこから読む
_PLAN_STEP_MAX_ITERATIONS = config.plan.step_max_iterations if config else 5
```

config オーバーライドはオプション（SMALL コスト）だが、意図的に短いステップを望むプロジェクト（例: 読み取り専用リサーチ）や長いステップが必要なプロジェクト（例: マルチファイル編集）に有用。

---

## 対象ファイル

| ファイル | 変更内容 |
|---|---|
| `src/reyn/chat/planner.py` | `_PLAN_STEP_MAX_ITERATIONS` 定数 |
| `src/reyn/config.py` | `PlanConfig.step_max_iterations`（オプション） |

---

## Dependencies

定数変更のみならなし。config 統合は `src/reyn/config.py` に `PlanConfig` が存在するか作成されることに依存する。

---

## Cost estimate

SMALL — 定数変更は 1 行。Config 統合はオプションで ~5 行の追加。

---

## Verification

1. 4 ops が必要なプランステップ（list_dir + 2 回 read + narrate）を構築 → サイレントにアボートせずステップが完了する。
2. `planner.py` で `_PLAN_STEP_MAX_ITERATIONS` = 5 を確認。
3. （オプション）`reyn.yaml` で `plan.step_max_iterations: 3` を設定 → バジェットが 3 に戻る。

---

## Related

- `src/reyn/chat/planner.py` — `_PLAN_STEP_MAX_ITERATIONS`、`_MAX_ROUTER_ITERATIONS`
- FP-0027 (`0027-plan-step-failure-transparency.ja.md`) — このバジェット到達による失敗が合成に転送されるようになる
