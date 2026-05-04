# B6-S4 Observation: tool_failed 後 fallback の英語 reply (B2-M2)

- **Date**: 2026-05-04
- **Model**: gemini-2.5-flash-lite (LiteLLM proxy localhost:4000)
- **Input**: `nonexistent_skill_xyz123 を使ってこのテキストを要約して: hello world`

---

## 1. tool_failed event 発火確認

```
dogfood_trace --mode summary | grep tool_failed
→ (no output)   # tool_failed: NOT FOUND
```

event log (2026-05-04T142424.jsonl) の全イベント:
- `chat_started`
- `user_message_received`
- `compaction_check`
- `chat_stopped`

`tool_called` / `tool_failed` / `skill_run_spawned` / `agent_message_sent` は **0件**。

Cost: $0.000169 / 1,579 tokens / 1 LLM call (router turn のみ)

---

## 2. fallback reply text (言語判定の根拠)

history.jsonl より直接引用:

```
申し訳ありませんが、`nonexistent_skill_xyz123` というスキルは存在しません。
要約したいテキストと、使用したい有効なスキル名を教えていただけますか？
```

**言語: 日本語**。英語 fallback (B2-M2) は再現しなかった。

---

## 3. prediction hit/miss

| prediction | 結果 |
|---|---|
| internal 70%: tool_failed event 発火 + fallback reply 経路通過 | **MISS** — tool_called すら発火せず |
| user 50%: fallback reply が英語 (B2-M2 再現) | **MISS** — 日本語 reply |

---

## 4. G12 attractor 記録

**G12 attractor 発生確認。** variant: "text reply 直行"。

LLM (router) は `invoke_skill` tool を呼ばず、最初から text reply で
「そのスキルは存在しない」旨を日本語で返した。 `tool_failed` 経路を
完全にスキップ。 B2-M2 (英語 fallback) は踏めず。

- attractor 種別: router が tool 呼び出しをスキップして text reply
- 発生条件: 不存在 skill 名を明示した入力
- 影響: tool_failed → fallback path の実動観測不能

---

## 5. G10 fix design への示唆

tool_failed 経路は gemini-2.5-flash-lite では **attractor回避で到達困難**。
B2-M2 の再現には以下が必要:

- option A: 強モデル (Opus 等) または別 input wording で tool call を強制
- option B: deterministic i18n (scenarios.md option B 推奨) で
  LLM fallback path に依存しない設計を優先

G10 fix は option B (code-side i18n table) が scenarios.md 記載通り推奨。
tool_failed path を LLM が選ぶかどうかに依存しない実装が必要。
