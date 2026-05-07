# Plan mode

複雑な chat query を独立 sub-task に分解し、 narrow LLM call で各
step を実行、 最終結果を user に返す仕組み。 process crash でも長
plan が中断されずに復元される。

## Plan mode とは

multi-source synthesis や compare-and-contrast 等の複雑な query で、
chat router が `plan` tool を呼んで構造化された分解を作る:

```
user query → planner LLM
                ↓
              [plan: 2-7 step + tools + 依存関係]
                ↓
              executor: 各 step が narrow LLM call で走る
                ↓
              terminal step の text → user reply
```

各 step は小さく focused な system prompt + 親 tool catalog の
subset で動く。 これにより full router prompt + 14 tool を全 sub-
task で持ち回る per-call context bloat を回避。

Plan mode は **query 単位 opt-in** — router LLM が分解する価値ある
query を見て `plan` を呼ぶ。 単純 query (= "hello" / 単一 tool call)
は経由しない。

## 非同期 dispatch

`plan` は async tool として登録されている。 router LLM が呼んだ時
chat turn は block しない:

1. `dispatch_plan_tool` が plan を validate、 `plan_id` + per-plan
   `chain_id` を allocate、 decomposition artifact を書き出し、
   `PlanRuntime` task を spawn。
2. router loop は async tool result を見て exit、 chat turn 終了。
3. plan task は background で実行、 step 完走ごとに status message
   を outbox に流して user に進捗が見える。
4. terminal aggregator step の text が通常の agent message
   (`kind="agent"`、 `meta.plan_id` で識別) として user に届く。

「人間と同じく quick reply が先、 長い work は background で続行」
の dispatch model。 plan in-flight 中に user は別 message を送れ、
複数 plan が並行実行可能。

## Crash に耐える state

| State | 場所 | crash 跨ぎ |
|---|---|---|
| Decomposition (= plan 形状) | `agents/<name>/state/plans/<plan_id>/decomposition.json` | 残る |
| Per-plan progress (= 完走 step + 結果) | `agents/<name>/state/plans/<plan_id>.snapshot.json` | 残る |
| Step output (≤32 KB) | 上記 snapshot に inline | 残る |
| Step output (>32 KB) | `agents/<name>/state/plans/<plan_id>/step_results/<step_id>.txt` | 残る |
| Plan lifecycle event | `.reyn/state/wal.jsonl` (`plan_started` / `plan_step_*` / `plan_completed`) | 残る |
| Active asyncio.Task | in-memory only | 失う — restart で auto-resume |

32 KB を超える step output は per-plan workspace file へ spill する
([ADR-0024](../decisions/0024-plan-step-result-spill.md)) ので、
snapshot は小さいまま + truncation なしで完全 preserve。 read 側は
`get_step_result(snap, agent_state_dir, step_id)` accessor が inline
/ spilled を透過解決。

`reyn chat` 起動時 `AgentRegistry.restore_all` が:

1. WAL を replay して各 agent の snapshot に反映
2. `active_plan_ids` の各 plan について `_recover_plans_for_agent`:
   - per-plan snapshot を load
   - decomposition artifact を読み込み (= P5 SSoT、 planner LLM 再
     呼びは決定論性なし → step ID が変わって memo 壊れる)
   - resume coordinator (= analyzer + policy) で各 step を classify
     (`pending` / `completed_with_result` / `failed` /
     `interrupted_with_child`)
   - `PlanRuntime` task を `resume_plan` 付きで spawn → 完走済 step
     は memo replay (= LLM cost ゼロ)、 pending step だけ再実行

長 plan の resume で LLM token を再課金しない。

## Multi-plan + 完走順応答

複数 plan が同時 in-flight 可能。 各 plan は独立した `plan_id` +
`chain_id` + decomposition dir を持つ。 outbox message は **完走順**
(= 依頼順ではない)、 `meta.plan_id` で識別。 30 秒 plan が 5 分 plan
より先に完走すれば先に user に届く。

WAL truncation floor は全 active plan の `last_step_applied_seq` を
含むので、 plan 進行中に resume analyzer が必要とする step event が
誤 truncate されない。

## Resume policy

`reyn.yaml` で coordinator の挙動を設定:

```yaml
plan_resume:
  default: retry_pending       # retry_pending | discard
  child_purity:                # plan step が child skill spawn した時
    pure:        cancel        # 冪等 + 安価 → 再実行
    world:       adopt         # child 自身の resume 機構に委ねる
    side_effect: adopt
    external:    adopt
    llm:         adopt
```

- `retry_pending` (default) — 完走 step は memo、 残りを再実行
- `discard` — plan を abort、 cancel flag 付き child を停止、 outbox
  に user 再依頼 notice

decomposition artifact が無い / 壊れている plan は coordinator が
auto-discard + 説明的 outbox notice。 planner LLM 再呼びはしない。

## Operator command

```
/plan list                                — active plan 一覧 (running + resume pending)
/plan discard <plan_id>                   — abort + state cleanup
/plan resume <plan_id> --from <step_id>   — 特定 step から再実行
```

`/plan discard` は asyncio.Task を cancel、 WAL に `plan_aborted` を
記録、 decomposition artifact + snapshot を削除、 plan の chain で
待っている peer agent に R-D14 notify。

`/plan resume --from` は ADR-0023 §3.7 surgical escape hatch。 step が
結果を記録したが operator が再実行したい場合 (= LLM 出力誤り / world
state が変動して record 結果が陳腐化) 用。 handler の動作:

1. 該当 plan の in-flight task を cancel
2. decomposition artifact を load して topological step 順を取得
3. `<step_id>` 以降の `step_results` / `step_failures` /
   `spawned_skill_run_ids` を clear (= 前 step は preserve)
4. `resume_plan` を再構築して通常 auto-resume path で起動 — 前 step は
   memo replay (= LLM cost ゼロ)、 残り step を再実行

未知 plan ID / decomposition artifact 欠落 (= `/plan discard` に誘導) /
plan に存在しない step ID (= 有効 step ID 列挙) は明示エラー。

## Crash 分類

[skill resume](skill-resume.md) と同じ exception-aware finally
pattern:

| Exit | 結果 |
|---|---|
| 正常 return | `plan_completed` 記録、 artifact 削除、 user に terminal text 配送 |
| `WorkflowAbortedError` | clean abort 扱い、 artifact 削除 |
| 一般 `Exception` / `KeyboardInterrupt` | `plan_run_interrupted` event、 artifact 残置、 restart で auto-resume |
| `kill -9` | `finally` skip、 artifact 残置、 restart で auto-resume |

artifact 残置 invariant が resume の前提 — artifact が消えると
coordinator は plan 形状を復元できず discard へ fallback。

## Cross-references

- [skill resume](skill-resume.md) — 兄弟設計、 plan は同じ WAL +
  snapshot + analyzer + coordinator pattern を再利用
- [permission model](permission-model.md) — plan step は per-step
  narrow tool catalog で動く
- [events](events.md) — `plan_*` / `plan_step_*` audit trail
- ADR-0022 (Phase 1 fail-safe)、 ADR-0023 (Phase 2 forward replay +
  Phase 2.1 async dispatch)
