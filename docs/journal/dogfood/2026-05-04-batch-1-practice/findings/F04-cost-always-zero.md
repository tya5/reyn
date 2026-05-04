# F4 [LOW]: cost、 永遠の 0

> 一行で: LLM 応答は来てるのに `cost -- prompt=0 completion=0 total=0`。
> 永遠の 0 円。

| Field | Value |
|---|---|
| Severity | LOW |
| Status | deferred (Wave B coverage audit で実施) |
| Scenario | scenario 1 (= 全 scenario で同様) |
| Found | 2026-05-04 |

---

## 観測

scenario 1 完了後、 chat の最後に毎回:

```
cost --  prompt=0 completion=0 total=0
```

LiteLLM proxy 経由なので token カウントが取れていない可能性。 LLM 応答は
正常に来ている (= 課金は発生しているはず) が、 reyn 側の表示は永遠の 0。

## 影響

- BudgetTracker (PR22 + R-D8) の永続化が landed したが、 入力 0/0/0 で
  集計しても意味がない
- user が「どれくらい使ったか」 を chat で確認できない

## Cause hypothesis

- LiteLLM proxy の response に `usage` field が含まれていない
- もしくは reyn の cost parser が proxy response 形式に対応していない
- もしくは litellm SDK が proxy response から usage を抽出できていない

## 優先度

LOW。 機能の正しさには影響しないが、 BudgetTracker ↔ LiteLLM proxy
組み合わせの dogfood で初めて顕在化した integration 問題。 別 issue で
追跡。
