# B2-M3 [MED]: MCP teardown で `RuntimeError: cancel scope in a different task`

> 一行で: `read_local_files` 成功後の MCP client teardown で anyio の
> cancel scope 違反 RuntimeError が stderr に残る — skill 出力は正常だが
> unretrieved Task exception がある。

| Field | Value |
|---|---|
| Severity | MED |
| Status | open |
| Scenario | S2 (Agent B — `read_local_files` + MCP) |
| Found | 2026-05-04 |

---

## 観測 (Agent B raw report)

S2 Run 2 (permission 事前承認後、 skill 成功パス) の stderr:

```
RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
```

スタックトレースは anyio → MCP client ライブラリ内。 skill の output
(`files_read: [README.md]`) は正常、 narrator reply も日本語で返却。
ただし Python の asyncio task に unretrieved exception が残る。

## 期待との差

skill 成功時は teardown も clean に完了するはずだった。
anyio / MCP client は cancel scope の task affinity を要求するが、
Reyn の MCP wrapper が teardown を別 task から呼んでいる可能性。

## 6 軸分析

| 観点 | 観測 |
|---|---|
| **意図解釈** | skill 動作は正常 |
| **応答品質** | user への reply は正しい |
| **待ち時間** | 影響なし |
| **見せ方** | stderr に trace が出るが CUI 表示には出ない |
| **エラー UX** | user には見えない (headless ログとして残る) |
| **state 整合性** | task exception が未回収 → 長期 session で accumulate するリスク |

## Severity guess

**MED** — 現時点では user 体験に影響しないが、 長期 session や並行 MCP
呼び出しで task exception accumulate が resource leak に化ける可能性がある。
anyio + MCP client の teardown パターンを `src/reyn/op_runtime/` の MCP
ハンドラで修正が必要 (cancel scope を `async with` から抜ける前に
同一 task で閉じる)。

## Reproduction notes

```bash
# S2 setup (permission pre-approved) で read_local_files を実行
# stderr を確認: "Attempted to exit cancel scope" が出ること
# WAL: skill_completed → success が確認できても stderr は出る
```
