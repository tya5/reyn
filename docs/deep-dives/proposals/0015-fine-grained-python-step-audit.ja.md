# FP-0015: Python step の per-call audit (双方向 RPC)

**Status**: **deferred** (= 具体的 enterprise audit 要件発生待ち)
**Proposed**: 2026-05-11
**Author**: 2026-05-11 設計議論 (FP-0014 の ADR-B から split out)
**Trigger**: FP-0014 が Scope A (= subprocess-local helper、 step-level
audit only) を採用したのは、 `python_runner.py` → `reyn.kernel._python_harness`
が使う `subprocess.run` boundary を contextvars が越えないため。 Scope B
(= per-call audit) は parent/child 間の双方向 RPC channel が前提 — それ自体が
独立した設計課題なので、 FP-0014 を Scope B 無しで ship できるよう本 FP に分離。

---

## Summary

`reyn.unsafe.*` helper を拡張し、 各 I/O call を subprocess 内 stdlib 実行から
**parent の run_op dispatcher への RPC** に切替。 author 視野 API
(= `reyn.unsafe.file.read(...)`) は不変、 wrapper body だけ `open(path).read()` から
`dispatch_op("file", verb="read", ...)` に差し替え。

Win: 各 I/O call が declarative run_op step と同等の audit (= per-call permission
gate / event emission / LLMReplay capture)。 Cost: parent/child JSON channel 上の
双方向 RPC protocol + parent round-trip 分の per-call latency。

---

## Motivation

FP-0014 Scope A の audit 細粒度は **step 単位**: python step 起動時 parent が
`python_started` (= function 名 + mode) を emit、 return 時 `python_completed`
(= 結果) を emit。 subprocess 内の挙動は parent audit log に opaque。 現状の
`mode: trusted` と同等なので regression ではない。

ただし enterprise audit が以下を要求する場合に応えられない:

- 「このスキルが読んだ全 file path を見せて」
- 「ネットワーク access は許可してるが、 この specific URL は block」
- 「audit log から実行を byte-for-byte で再現」

これらは per-invocation 可視性が必要、 Scope B がその path。

## Proposed implementation

### Component A — 双方向 JSON channel

現 harness protocol は one-shot: parent stdin → request、 child stdout → response、
以上。 これを **専用 channel 上の length-prefixed 双方向 framing** に拡張
(= side socket / `pipe` pair / 追加 fd)。 stdin/stdout は元 request/response
envelope 用に残置 (= backward compat)。

```
Frame format (双方向共通):
  4-byte big-endian length || JSON payload

Child → Parent (dispatch request):
  {"kind": "op_dispatch", "id": "...", "op": "file", "args": {...}}

Parent → Child (dispatch response):
  {"kind": "op_result", "id": "...", "ok": true, "result": ...}
  または
  {"kind": "op_result", "id": "...", "ok": false, "error": ..., "kind": ...}

Child → Parent (terminal):
  {"kind": "step_result", "ok": true, "result": ...}
  (= 現 stdout response と同 envelope)
```

`id` は並行 in-flight call (= helper が thread で並列 I/O) の dispatch
request と result の相関に使う。

### Component B — In-child RPC client

`reyn.api._internal.dispatch_op` を local stdlib call から RPC に切替:

```python
# Scope B: helper body が local stdlib call から dispatch RPC に
def read(path: str, *, encoding: str = "utf-8") -> str:
    return _dispatch_op("file", verb="read", path=path, encoding=encoding)
```

`_dispatch_op` 実装は `reyn.api._internal` 配下、 request frame を serialize、
RPC channel に write、 response frame で blocking、 RPC error / op failure で
raise。

### Component C — Parent RPC server loop

Parent の `PythonRunner.run` を one-shot `subprocess.run` から、 child 実行
中の RPC channel を並行 read する streaming reader に拡張。 各 `op_dispatch`
frame に対し:

1. Frame を parse。
2. 既存 `dispatch_op` machinery で op kind を lookup。
3. `PermissionResolver.require_*` を call (= per-call permission gate)。
4. `op_started` / `op_completed` event を emit (= per-call audit)。
5. Result frame を RPC channel に write back。

Cancellation (= step timeout が RPC 中に発火) は clean に child を kill。

### Component D — Per-call permission gate

現状の `python.unsafe` permission は **step 全体** に対する grant。 Scope B
では `op_dispatch` ごとに specific op + args に対する `require_*` check が
発火。 2 つの policy 選択肢を ADR で決定:

- **Inherit-from-step**: child step が既に `mode: unsafe` 承認済 → 全
  `op_dispatch` pass。 Scope A と同等の effective granularity だが audit
  emission は per-call。
- **Re-gate**: 各 `op_dispatch` を skill 宣言 permission (`permissions.file_read`
  / `permissions.http` 等) に re-check。 厳格な granularity、 declarative
  run_op step と一致。

Re-gate のほうが rigorous、 inherit-from-step のほうが simple。

## Open design questions (ADR delegate)

1. **ADR-A: Channel 実装**。 side socket / `subprocess.Popen(pass_fds=...)`
   経由の追加 pipe pair / stdin/stdout 上 multiplexed framing。 `pass_fds`
   は Linux/macOS portable、 socket 不要、 clean な separation。
2. **ADR-B: Permission gate policy**。 inherit-from-step vs re-gate
   (= Component D 参照)。
3. **ADR-C: Concurrency model**。 parent は dispatch を sync で serve
   (= 1 RPC at a time) するか、 concurrent (= helper が thread / async
   をサポート) するか。 Concurrent のほうが capable だが replay 用に
   serialisable ordering が必要。
4. **ADR-D: LLMReplay integration**。 各 `op_dispatch` が declarative
   run_op record と同 `(skill, phase, step, call_idx)` tuple で replay
   capturable record を produce すべき。 schema migration 必要。
5. **ADR-E: Scope A との backward compat**。 1 つの `reyn.unsafe.*`
   package が parent capability advertisement で両 mode (= local stdlib
   vs RPC) を serve するか、 別 package version として ship するか。

## Dependencies

- **FP-0014 (= LANDED 前提)** — `reyn.unsafe.*` namespace +
  `reyn.api._internal.dispatch_op` 抽象点を提供、 Scope B が内部 差替。
- **`PermissionResolver` per-call API** — 既存、 変更不要。
- **Events store schema** — declarative run_op 用の `op_started` /
  `op_completed` schema は既存、 そのまま reuse。
- **LLMReplay capture** — python step 内 per-call op invocation の記録に
  拡張。

## Cost estimate

**MEDIUM** (~3-4 day)。

| 項目 | 見積もり |
|---|---|
| 双方向 JSON channel + framing | ~1 day |
| In-child `_dispatch_op` RPC client | ~0.5 day |
| Parent RPC server loop integration | ~1 day |
| Permission policy + event emission | ~0.5 day |
| LLMReplay schema migration | ~0.5 day |
| ADR drafting (A-E) | ~0.5 day |
| `reyn.unsafe.*` wrapper body migration | ~0.5 day |
| Tests (Tier 2 + Tier 3 e2e) | ~0.5 day |

Stdlib refactor は不要 (= FP-0014 後は既に safe mode で動作)。 User 側
`mode: unsafe` skill は package version up で自動的に new audit 細粒度を取得。

## Risks

- **Latency overhead** — 各 helper call が parent round-trip 分の cost。
  I/O-heavy で call 回数少なめの step は許容、 tight loop は遅い。
  緩和策: per-skill opt-out として Scope A を残す
  (= `audit_level: step` vs `audit_level: per_call`)。
- **Cancellation correctness** — step timeout が RPC 中に発火、 parent state を
  leak せず child を clean kill する必要。 緩和策: channel は parent 所有、
  child kill 時に tear down。
- **Replay determinism** — concurrent RPC は全順序 capture が必要、 replay で
  同 call sequence を再現する必要。 ADR-C で解決。

## When to revisit

本提案を `deferred` → `accepted` に flip する **trigger 条件**:

- Per-invocation gating を name する具体的 enterprise customer audit 要件
  (= 「読まれた全 file path を知る必要がある」)。
- Step-level audit が Reyn の threat model に不十分という security review
  finding。
- 今の step-level replay が non-deterministic helper internals で drift する
  case の replay 信頼性 work が op-level replay を要求。

これら trigger 発火まで FP-0014 の Scope A で十分。

## Related

- **FP-0014 (= 前提、 LANDED)** — Scope A (= step-level audit) を採用、
  本提案が拡張する `reyn.unsafe.*` namespace を future hookup point として
  残置。
- **ADR-0026 unified tool registry** — declarative run_op は本 FP が python
  step に持ち込む per-call audit インフラを既に保有。
- **`docs/concepts/events.md`** — events emission model。
- **`docs/reference/testing/replay.md`** — LLMReplay scope、 本 FP で
  op-level に拡張。
