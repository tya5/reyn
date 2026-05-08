# B2-M3 Root Cause Investigation: MCP teardown anyio cancel scope (G11)

| Field | Value |
|---|---|
| Status | investigation complete |
| Source | `src/reyn/mcp_client.py` + `mcp/shared/session.py` + batch 2-3 WAL |
| Date | 2026-05-04 |

---

## 観測

### traceback (B3-S4 / B2-M3 共通)

```
Unhandled exception in event loop:
  File ".../mcp/client/stdio/__init__.py", line 183, in stdio_client
  Exception: Attempted to exit cancel scope in a different task than it was entered in
```

- B2-M3 (batch 2 S2): `read_local_files` 成功後に stderr に出現
- B3-L3 (batch 3 S4): B2-M3 再現として明記 (B3-S4-observation.md 末尾)
- B4-S4: giveup-tracker G11 に「batch 4 でも再現」 として記録

### MCP lifecycle の実装 (src/reyn/mcp_client.py)

`MCPClient.initialize()` では `AsyncExitStack` に 2 段で context manager を積む:

1. `stack.enter_async_context(stdio_client(params))` — anyio task group を内包
2. `stack.enter_async_context(ClientSession(read_stream, write_stream))` — 別の anyio task group を内包

`MCPClient.close()` は `await stack.aclose()` を呼ぶだけで、呼び出し task は caller に依存する。

### anyio の cancel scope task affinity 制約

`mcp/shared/session.py` の `BaseSession.__aenter__` は:

```python
self._task_group = anyio.create_task_group()
await self._task_group.__aenter__()
self._task_group.start_soon(self._receive_loop)
```

`BaseSession.__aexit__` は:

```python
self._task_group.cancel_scope.cancel()
return await self._task_group.__aexit__(exc_type, exc_val, exc_tb)
```

anyio の cancel scope は **enter した task と exit する task が同一でなければならない** (task affinity)。`AsyncExitStack.aclose()` が別 task から呼ばれると `_cancel_scope.__exit__` が task affinity 違反で RuntimeError を raise する。

### `_mcp_clients` の lifetime (src/reyn/kernel/control_ir_executor.py)

`ControlIRExecutor.__init__` で `self._mcp_clients: dict = {}` を保持し、複数 phase をまたいで再利用する。`execute()` ごとに `OpContext` に渡しているが、**skill run 完了後に `_mcp_clients` を close する処理は存在しない**。

実際の close は skill run 外部 — GC か process 終了時に `MCPClient.__del__` が呼ばれるか、あるいは `AsyncExitStack` の `__del__` で finalize される。GC が動くのはメインの asyncio event loop task とは異なる文脈になりうる。

---

## 仮説 list

### 仮説 A: `stack.aclose()` が asyncio GC task から呼ばれる (確度: 高)

**メカニズム**: `MCPClient` / `AsyncExitStack` が GC で回収される時、Python の `__del__` → asyncio が "destroy" コールバックを別 future で schedule する。これが元の task とは無関係な GC コンテキストから `_cancel_scope.__exit__` を呼び出し、anyio の task affinity チェックが RuntimeError を raise する。

**evidence**:
- エラーが `Unhandled exception in event loop` として出る (= future の exception が未回収)
- skill 正常完了後に遅延して出現する (= GC タイミング依存)
- `_mcp_clients` の明示的 close が存在しない (`control_ir_executor.py` に teardown なし)

**検証コスト**: 低 — skill run 完了直後に `for c in self._mcp_clients.values(): await c.close()` を追加して stderr が消えるか確認。

---

### 仮説 B: `stdio_client` の anyio task group が `AsyncExitStack.aclose()` 経由で cross-task に exit される (確度: 高)

**メカニズム**: `stdio_client` は内部で `anyio.create_task_group()` として `tg` を作り、`stdout_reader` / `stdin_writer` を spawn する。この `tg` の cancel scope は `stdio_client` が enter された asyncio task に紐づく。`stack.aclose()` が別の task (または GC) から呼ばれると、`tg.__aexit__` が別 task から実行され anyio task affinity 違反になる。

**evidence**:
- traceback が `mcp/client/stdio/__init__.py:183` を指す (= `anyio.create_task_group()` の exit 箇所)
- `stdio_client` は `@asynccontextmanager` で `async with anyio.create_task_group() as tg:` を使う
- `AsyncExitStack` は LIFO で exit を呼ぶが、呼び出し task は `aclose()` の呼び元に依存

**検証コスト**: 低 — 仮説 A の修正 (明示的 close を skill run 完了直後に同 task で呼ぶ) で同時検証可能。

---

### 仮説 C: `ClientSession` の `_receive_loop` task が teardown 順序の問題で cancellation を受ける (確度: 中)

**メカニズム**: `BaseSession.__aexit__` は `_task_group.cancel_scope.cancel()` → `_task_group.__aexit__()` の順に実行する。しかし `stdio_client` の `tg` (外側) が先に exit しようとすると、`_receive_loop` が動いている状態で `read_stream` が close され、`_receive_loop` が `ClosedResourceError` → 別 exception propagation 経路に入る可能性がある。

**evidence**:
- `BaseSession.__aexit__` は `cancel_scope.cancel()` 後に `tg.__aexit__` を待つ設計
- `stdio_client` の `finally` ブロックで `read_stream.aclose()` を呼ぶ順序が `ClientSession.__aexit__` より先になりうる

**検証コスト**: 中 — 仮説 A/B の修正で消えない場合に調査。teardown 順序ログ追加が必要。

---

## 推奨 fix order

1. **仮説 A+B を同時検証**: `ControlIRExecutor` の `execute()` または専用 `teardown()` メソッドで、skill run 完了後に **同 task** で `for c in self._mcp_clients.values(): await c.close(); self._mcp_clients.clear()` を呼ぶ。stderr が消えれば A+B 確定。
2. 消えない場合 → 仮説 C: `MCPClient.close()` 内で `ClientSession.__aexit__` の前に `stdio_client` tg を cancel する順序制御を検討。

---

## Out of scope

- 大規模 MCP refactor (= 別 PR)
- anyio version 上げ等の依存変更
- `http` transport の調査 (= `streamablehttp_client` は別実装、別観測が必要)
