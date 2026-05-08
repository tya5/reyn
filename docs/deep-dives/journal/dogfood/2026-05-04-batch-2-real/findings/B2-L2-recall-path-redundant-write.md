# B2-L2 [LOW]: recall 時に `remember_shared` を再度呼んでから read — 無駄 + drift リスク

> 一行で: turn 2 の recall path で LLM が `remember_shared` を呼んでから
> `read_memory_body` を呼ぶ — 不要な上書きで frontmatter drift が起きた。

| Field | Value |
|---|---|
| Severity | LOW |
| Status | open |
| Scenario | S5 (Agent A — memory remember+recall) |
| Found | 2026-05-04 |

---

## 観測 (Agent A raw report)

turn 2 (`"私について何か知ってることある？"`):

1. LLM が `remember_shared` を call (上書き) — frontmatter `name` が
   `"Tetsuya's preferences"` → `"Tetsuya preferences"` にドリフト
2. 続いて `read_memory_body` を call
3. 読み取り内容を基に reply 生成: 正常

## 期待との差

recall path では `read_memory_body` (または memory index 参照) のみで
十分なはずだった。 turn 2 で `remember_shared` を呼ぶ必要はない。

推測: LLM が「覚えておく必要があるかもしれない」 と判断して先に書いてから
読む、 という過剰な保険行動をとっている。 memory tool の description や
router prompt が「いつ書き、 いつ読むか」 を明示していないため。

## 6 軸分析

| 観点 | 観測 |
|---|---|
| **意図解釈** | recall 意図は正しく達成 |
| **応答品質** | reply は正しい |
| **待ち時間** | 1 余分な write call |
| **見せ方** | tool call 2 つが CUI に出る |
| **エラー UX** | エラーなし |
| **state 整合性** | frontmatter に minor drift (name field) が記録される |

## Severity guess

**LOW** — 今回は minor drift (name field のみ)。 ただし content field も
drift した場合は memory 汚染になりうる。 `remember_shared` の tool description
に「新情報があるときのみ呼ぶ」 を明示する。 あるいは read-before-write
check を OS 側で強制する (permissiveness によっては別設計)。
