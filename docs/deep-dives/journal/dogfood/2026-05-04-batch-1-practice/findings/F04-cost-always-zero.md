# F4 [LOW]: cost、 永遠の 0

> 一行で: LLM 応答は来てるのに `cost -- prompt=0 completion=0 total=0`。
> 永遠の 0 円。

| Field | Value |
|---|---|
| Severity | LOW |
| Status | **fixed** at `70194d5` + residual fix at `d9e5fce` |
| Scenario | scenario 1 (= 全 scenario で同様) |
| Found | 2026-05-04 |
| Fixed | 2026-05-04 (+ residual 2026-05-04) |

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

---

## 修正 (2026-05-04)

**2 つの独立した bug** が原因と判明 (Agent E 調査)。

### Bug 1 — proxy prefix がついたまま estimate_cost を呼んでいた

`kernel/runtime.py` の `_call_llm_and_record` は `resolved_model =
"openai/gemini-2.5-flash-lite"` のまま `estimate_cost()` に渡していた。
`litellm.model_cost` のキーは `"gemini-2.5-flash-lite"` (bare) なので
lookup が `(None, None)` を返し、 `_total_cost_usd` が 0 のまま。

同様に `llm.py` の `call_llm` / `call_llm_tools` も `budget.record_llm(model=model,
...)` へ未 strip の文字列を渡していた。 `BudgetTracker.record_llm` 内部でも
`estimate_cost` を呼ぶため、 persistent budget ledger の cost も 0 だった。

**修正**: `_call_llm_and_record` と `_credit_budget_from_memo` に

```python
_pricing_model = (
    resolved_model.split("/", 1)[1]
    if "/" in resolved_model and _proxy_kwargs()
    else resolved_model
)
```

を追加し、 `estimate_cost(_pricing_model, ...)` を呼ぶよう変更。
`call_llm` / `call_llm_tools` は `effective_model` (already stripped) を
`budget.record_llm` へ渡すよう変更。

### Bug 2 — RouterLoop.run() が None を返していた

`RouterLoop.run()` の戻り値は `None` だったため、 router の LLM call usage が
`ChatSession._total_usage` に積み上がらなかった。 stdlib skill (narrator 等)
の usage だけが集計される状態。

**修正**:
- `RouterLoop.__init__` に `self._total_usage = TokenUsage()` を追加。
- `RouterLoop.run()` が各 `call_llm_tools` の `result.usage` を
  `self._total_usage` に加算し、最後に `TokenUsage` を返すよう変更。
- `ChatSession._run_router_loop` が返り値を受け取り、
  `self._total_usage += router_usage` + `estimate_cost` + `self._total_cost_usd +=`
  を実行。

### 検証

Tier 2 invariant test 3 件を `tests/test_session_cost_accumulation.py` に追加:
- `test_router_loop_total_usage_propagates_to_session` — Bug 2 の回帰防止
- `test_estimate_cost_strips_proxy_prefix` — Bug 1 の litellm lookup を直接確認
- `test_router_loop_run_accumulates_usage_across_iterations` — 複数 iteration 集計

### Residual bug (2026-05-04 後追い verification)

`70194d5` 後の e2e (1 turn 実 LLM) で **依然として `cost=0`** が観測された。
原因は Bug 1 の修正対象とは別の経路:

`session.py` の RouterLoop cost 集計部で `self._resolver.resolve("router")` を
呼んでいたが、 文字列 `"router"` は model alias mapping
(`light` / `standard` / `strong`) に存在しないため resolver は
**literal "router"** を返す。 これに `/` が含まれないので proxy prefix
strip 条件をスキップし、 `estimate_cost("router", usage)` が litellm の
model_cost lookup に失敗 → `(None, None)` 返却 → `_total_cost_usd=0` のまま。

**修正** (`d9e5fce`): `resolve("router")` → `resolve(loop.router_model)`
(= `resolve("light")`) に変更。 `light` は mapping に存在し
`"openai/gemini-2.5-flash-lite"` を返すので prefix strip が動き、
`estimate_cost("gemini-2.5-flash-lite", ...)` が正しく $0.0002 を返す。

**e2e 検証**: `reyn chat default --cui --no-restore` で「こんにちは」 1 turn
→ chat exit summary で `cost $0.0002 prompt=1601 completion=11`。 全数値 > 0。

このような「Tier 2 で個別 module を pin しても e2e で組み合わせると未発見経路が
ある」 タイプの bug は、 dogfood の後追い verification で初めて顕在化した。
教訓: cost 系の verification は scenario 内に `:cost` slash 確認を組み込むべき。
