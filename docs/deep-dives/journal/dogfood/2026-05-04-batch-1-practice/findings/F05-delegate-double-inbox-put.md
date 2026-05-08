# F5 [HIGH]: delegate、 言ってないのに 2 回送る

> 一行で: LLM が `delegate_to_agent` を 1 回呼んだのに、 specialist の
> inbox には同じ依頼が **2 件** 届く。

| Field | Value |
|---|---|
| Severity | HIGH |
| Status | **fixed** at `9e8126c` |
| Scenario | scenario 2 (specialist agent → curry recipe) |
| Found | 2026-05-04 |

> Context: F5 は scenario 2 で発生した「16 秒の悲劇」 cascade の口火を
> 切った bug。 全体の流れは [findings.md の F5-F8 連鎖事故 section](../findings.md#f5-f8-multi-agent-delegate-完全失敗の四重奏-16-秒の悲劇)
> を参照。

---

## 観測

WAL trace:
```
08:26:24.745  inbox_put  target=specialist  msg_kind=agent_request
08:26:24.747  inbox_put  target=specialist  msg_kind=agent_request   ← 2 ms 差
```

events log でも `tool_called delegate_to_agent` が 2 件連続 (1 ターン内)。
LLM が同一 tool を 2 回 call しているか、 OS 側が 2 重 dispatch している。

`router_loop` で `delegate_to_agent` ハンドラの async 経路 (commit
`caaed75` で導入) を再点検する必要がある。 sync 経路と async 経路で event
emit が二重発火している hypothesis。

## 原因

LLM (gemini-2.5-flash-lite) が同一 tool_calls list 内に
`delegate_to_agent` を 2 回 emit していた。 RouterLoop は parallel に
asyncio.gather で全部 dispatch するため、 そのまま 2 回 inbox_put される。

OS 側の二重 dispatch ではなく **LLM 出力側の duplication**。 weak model で
時々発生する pattern。

## 修正 (commit `9e8126c`)

`RouterLoop._dedupe_async_tool_calls` を導入: 同一 round 内で
`(tool_name, arguments_json)` が一致する **async tool_calls** のみを
dedupe (sync tool dupes は wasteful だが correctness-preserving なので
触らない)。 抑制された call は `tool_call_deduped` 監査 event を emit。

3 件の Tier 2 test (positive / distinct-args negative / sync-scope guard) で
pin。

## 教訓 / 後続

- weak model の出力は不安定 — OS 側で防御するか、 strong model にする
  かは cost trade-off の判断
- 「sync tools は dedupe しない」 という scope 制限は将来の strong model
  で false positive を避ける defensive choice。 batch 2 で観察したら
  scope 拡大を検討
