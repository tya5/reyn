# B2-L1 [LOW]: `remember_shared` が同一 args で 2 回 fire (sync tool dupe)

> 一行で: turn 1 で `remember_shared` が同一 args_hash `dfa868c8eeeeb6af` で
> 2 回呼ばれる — F5 dedupe は async 専用のため sync tool には効かない。

| Field | Value |
|---|---|
| Severity | LOW |
| Status | open (Wave B) |
| Scenario | S5 (Agent A — memory remember+recall) |
| Found | 2026-05-04 |

---

## 観測 (Agent A raw report)

turn 1 (`"私は Python が好きで、 Reyn project を試している tetsuya です。これを覚えておいて"`):

- `remember_shared` が同一 args (`args_hash: dfa868c8eeeeb6af`) で **2 回** call
- memory file は 2 回書かれる (= last write wins で実害なし、 ただし double write)

## 期待との差

F5 dedupe (`_dedupe_async_tool_calls`) は async tool calls のみを対象。
sync tool が LLM に 2 回 emit されてもそのまま通る。 設計的には
「sync dupe は wasteful だが correctness-preserving」 としてスコープ外に
していた (F5 修正コメント参照)。

今回 `remember_shared` は sync tool のため F5 が適用されず、 LLM の 2 重
emit がそのまま 2 重 write になる。

## 6 軸分析

| 観点 | 観測 |
|---|---|
| **意図解釈** | 意図は正しい (「覚えて」 → remember_shared) |
| **応答品質** | 最終的な memory 内容は正しい (last write wins) |
| **待ち時間** | sync 2 回なので若干 overhead |
| **見せ方** | CUI で tool call が 2 回表示され user が不思議に思う可能性 |
| **エラー UX** | エラーにはならない |
| **state 整合性** | events に 2 件 tool_called が記録される (= audit で puzzling) |

## Severity guess

**LOW** — 実害なし。 ただし sync dupe 防止の scope を F5 と共に広げるか、
LLM への tools description で「一度だけ呼べ」 旨を明示することで対処可。
Wave B coverage audit で他の sync tool dupe と合わせて整理。
