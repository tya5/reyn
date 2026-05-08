# Batch 4 Retest — Prelude

> Scope: batch 3 S1 + S2 の retest。 B3-H1 fix (`48676ad`) + B3-M1 fix
> (`d8328b2`) の効果を実 LLM で確認。 main HEAD `066d28d`。

## 目的

batch 3 で発見・fix した以下 2 件の動作確認:

| Fix | PR / commit | 内容 |
|---|---|---|
| B3-H1 | `48676ad` | `router_system_prompt.py` に「`list_skills` で matching skill を発見したら `describe_skill` か `invoke_skill` を呼ぶこと。 直接返答禁止」ルール追加 |
| B3-M1 | `d8328b2` | scenarios.md S1 に specialist agent 作成手順を追記 |

B3-M2 (router が `read_local_files` 明示でも `list_skills`/`invoke_skill` を呼ばず direct reply) は
B3-H1 と同 family のため likely 解消 → batch 4 で確認する方針。

## 実行環境

| 項目 | 値 |
|---|---|
| main HEAD | `066d28d` |
| Worktree | `.claude/worktrees/agent-a4fce3641d853c784` |
| LLM | `openai/gemini-2.5-flash-lite` via LiteLLM proxy `localhost:4000` |
| OPENAI_API_KEY | `dummy` |
| 実行日 | 2026-05-04 |

## batch 3 事前 prediction (再掲)

- S1「70% でカレー届く」 (B3-H1 fix 後)
- S2「40% で ask_user 観測」 (B3-M2 likely fix 後)

## 結果サマリ

| Scenario | 期待 | 実際 | Fix 効果 |
|---|---|---|---|
| S1 (curry) | カレーレシピが user に届く | specialist invoke_skill 成功、curry recipe 生成。しかし user に届かず (B4-H1 新規 HIGH) | partial |
| S2 (ask_user) | router が read_local_files を invoke | LLM 分散: 1回は list_skills 呼ぶ (partial fix 効果) / 1回は direct reply。ask_user は未発火 | partial |

詳細: `findings/B4-retest-S1-S2.md`
