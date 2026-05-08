# B2-M1 [MED]: router、 存在しない skill 名 `general.summarize` を召喚

> 一行で: F3 修正で router は invoke_skill を呼ぶようになったが、
> skill 名を hallucinate して `general.summarize` (どこにも存在しない) を指定。

| Field | Value |
|---|---|
| Severity | MED |
| Status | open |
| Scenario | S1 (Agent A — text 要約) |
| Found | 2026-05-04 |

---

## 観測 (Agent A raw report)

WAL から抜粋 (`chat/2026-05/2026-05-04T095827.jsonl`):

```json
{"type": "tool_called", ..., "tool": "invoke_skill",
 "args": {"input": {"count": 3, "text": "..."}, "name": "general.summarize"}}
{"type": "tool_failed", ..., "tool": "invoke_skill", ...,
 "message": "ValueError: skill 'general.summarize' not found; available: ['direct_llm', 'eval', 'eval_builder', 'judge_phase', 'mcp_search', 'read_local_files', 'skill_builder', 'skill_importer', 'skill_improver', 'word_stats_demo']"}
```

期待していた skill 名: `text_summarizer` (存在するが `reyn/local/` — worktree
gitignore 対象)。 LLM は `general.summarize` を発明した。

## 期待との差

F3 fix の目的は「router が invoke_skill を呼ぶこと」。 ✅ 呼んだ。
だが呼んだ skill 名が幻覚。 `list_skills` を呼ばずに名前を推測したと思われる。

## 6 軸分析

| 観点 | 観測 |
|---|---|
| **意図解釈** | invoke 意思は生まれたが skill 名選択が hallucination |
| **応答品質** | skill failure 後の fallback (= B2-M2) が出る |
| **待ち時間** | invoke 失敗 → fallback 2-turn |
| **見せ方** | tool_failed は CUI に表示される |
| **エラー UX** | error message は技術的だが user には謎 |
| **state 整合性** | tool_failed event 正常 emit、 skill_runs entry は出ない |

## Severity guess

**MED** — F3 「invoke しない」 よりは前進。 ただし `list_skills` を使って
existing skills を確認してから invoke するよう router prompt を強化する
必要がある (= 現状の prompt は「domain tasks → invoke_skill」 だが
「まず list_skills を呼べ」 が抜けている可能性)。

## Reproduction notes

```bash
reyn chat default --cui --no-restore
# user: "次の英文を 3 つの bullet point に要約して: ..."
# WAL grep: tool_failed invoke_skill → general.summarize が出れば再現
# 注: reyn/local/text_summarizer が存在する環境では挙動が変わる可能性
```
