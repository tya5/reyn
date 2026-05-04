# Batch 4 Retest — Findings

> B3-H1 (`48676ad`) + B3-M1 (`d8328b2`) fix 後の S1 + S2 再実行結果。
> main HEAD `066d28d`、 2026-05-04。

## 概要

| ID | 重要度 | 一行で言うと | 状態 |
|---|---|---|---|
| B4-H1 | HIGH | `_run_skill_awaitable` が narrator 結果を private `_put_outbox` 経由で push → `_router_loop_agent_replies` に捕捉されず specialist reply が user に届かない | open |
| B3-H1 | HIGH | specialist の `list_skills → stop` attractor | verified fixed (invoke_skill まで到達確認) |
| B3-M2 | MED | router が `read_local_files` 明示でも tool 呼ばない | partial fix (LLM 分散あり、ask_user は依然 dark) |

**HIGH 1 件新規** (B4-H1)。 B3-H1 は fix 効果確認 (specialist 側で invoke_skill 到達)。

詳細観測: [findings/B4-retest-S1-S2.md](findings/B4-retest-S1-S2.md)
