# F6 [HIGH]: specialist、 まだ答えてないのに「答えました」 を送る

> 一行で: specialist 側、 LLM がまだ考え中なのに「答えました (中身: 空)」
> を default に送りつける。

| Field | Value |
|---|---|
| Severity | HIGH |
| Status | **fixed** at `9e8126c` |
| Scenario | scenario 2 (specialist agent → curry recipe) |
| Found | 2026-05-04 |

> Context: F5-F8 連鎖事故 cascade の真ん中。 F5 で 2 重 dispatch、 F6 で
> 早期空 reply、 F7 で空 reply の誤解、 F8 で英語 fallback の連鎖。

---

## 観測

specialist 側 events log:
```
08:26:30  tool_called  describe_skill                ← まだスキル決まってない
08:26:34  agent_message_sent  kind=agent_response  response: ""
08:26:39  agent_message_sent  kind=agent_response  response: ""
08:26:42  agent_message_sent  kind=agent_response  response: "<実際のレシピ>"
```

最初の 2 つの空 reply は明らかに早期送出。 `router_loop` が tool 呼び出し
結果を受け取った時点で「とりあえず agent_response 送る」 挙動になっている
ように見える。 LLM の final answer 確定まで agent_response 送出を遅らせる
必要がある。

これは F5 と独立だが連鎖して悪化させる (= 2 重 request × 早期空 reply ×
2 と、 ノイズが指数的)。

## 原因

`_handle_agent_request` の RouterLoop 完了後の forwarding logic:

```python
reply_text = agent_replies[0] if agent_replies else ""
await self._send_agent_response(to=from_agent, response=reply_text, ...)
```

`agent_replies` は `put_outbox(kind="agent")` 時に **`text` が truthy なら**
capture。 つまり empty content の text reply は capture されない →
`agent_replies` 空 → 空文字列を upstream へ送信。

加えて max_iterations 枯渇時の "error" outbox や、 async dispatch 後の
"status" outbox も capture されない。 全部 empty 経路で upstream に "" 流す。

## 修正 (commit `9e8126c`)

`_no_reply_marker(agent_name, reason)` ヘルパー追加。 6 箇所の `response=""`
を構造化マーカー `[<agent>: could not produce a reply — <reason>]` に置換:

- `_handle_agent_request`: cap_exceeded / generic_exception / no_text_reply
- `_resolve_pending_chain`: 同 3 経路

マーカーは意図的に英語 + structural — 受信側 agent の LLM が解釈して
user の output_language で返答することを期待。 verbatim forward は別問題
(F11 系) で扱う。

## 教訓 / 後続

- 「空文字列で fallback」 は ambiguous default — 「失敗」 と「正常な空」
  の区別が付かない。 OS 抽象では explicit failure marker が望ましい
- output_language を通した receiving agent の reply 品質は batch 2 で観察
