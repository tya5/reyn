# FP-0013: 統合 Inbox/Outbox Transport 抽象化 — CUI vs MCP/A2A の skew を解消

**Status**: **accepted** (= ADR-A starvation feasibility green-light、 2026-05-11)
**Proposed**: 2026-05-11
**Author**: 2026-05-11 設計議論 (FP-0012 R-A2A-COMPLETION-DRAIN retest 後)
**Trigger**: FP-0012 retest F1 finding (= A2A endpoint が `session.run()` を bypass する
ため `skill_completed` inbox kind が A2A 駆動 agent では永久に fire しない問題)。 短期
patch (= commit `b3252be`、 `drain_skill_completed_inbox`) で immediate gap は塞いだが、
本提案が扱う **architectural skew** は残置している。

## Feasibility verification (2026-05-11)

5 track 並列調査で open question ADR-A を closure:

- **Track 1 (archaeology)**: bypass は commit `a5678c1` (2026-05-07) で empirically
  観測。 A2A はその ~45 分後に bypass を uncritically 継承 — uvicorn / pure asyncio
  surface のため、 元々問題なかった可能性。
- **Track 2 (mechanics)**: root cause は anyio task-group の structured-concurrency
  cancellation cascade + buffer-0 memory-stream rendezvous で、 generic asyncio
  unfairness ではない。 pumping は 2 task → 1 task に集約、 failure mode を
  mechanical に排除。
- **Track 3 (industry)**: request-handler pumping は industry standard (= LangGraph
  `astream` / Strawberry GraphQL subscription が direct precedent)。
- **Track 4 (baseline repro)**: 段階的に近い 3 harness (asyncio / anyio / 実
  `mcp.server.Server` + in-memory JSON-RPC) を構築。 3 全 pass、 starvation は
  **再現せず**。 subprocess + 実 stdio byte transport は未着手 — residual
  verification に deferred。
- **Track 5 (pumping prototype)**: `ChatSession.run_one_iteration` +
  `send_to_agent_impl_pumping` を実装。 4/4 spike test pass、 334 regression test
  green、 bypass より ~100ms 遅い。

Synthesis: `docs/deep-dives/journal/feature-verify/2026-05-11-adr-a-starvation-feasibility/synthesis.md`。

**Resolution**: green-light proceed。 Cost estimate 精緻化 — core decomposition は
SMALL-MEDIUM (~25+80 行); LARGE 見積もりは verification + soak が dominate する。
**bypass 削除 commit の precondition** として 3 件 residual verification 必要
(= 本提案 accept の前提ではない):

1. subprocess + 実 stdio probe (`scripts/mcp_probe.py` を pumping path 側で実行)。
2. anyio CancelledError soak (mid-call disconnect)。
3. `_receive_loop` heartbeat instrumentation 越し >5s LLM call。

**Naming refinement 採用**: pump primitive 名を
`session.run_until_reply(reply_to: TransportRef) -> OutboxMessage` に
(Track 3 提案、 LangGraph `astream` の `__anext__` mirror)。

**Migration ordering refinement**: A2A migration が先 (= Track 1 で bypass 不要と
判明); MCP は subprocess soak 後に follow。

---

## Summary

Reyn の設計 thesis: **agent は inbox 1 本 (= 入力) と outbox 1 本 (= 出力) を持ち、
sender / receiver の正体 (user / peer agent / MCP client / A2A peer) は agent 本体に対して
透過**。 agent の責務は inbox message を処理し、 `reply_to` envelope を付けて outbox に
出すこと。 実 wire 形式 (TUI / MCP stdio / A2A HTTP / peer-agent inbox) への配送は
**transport layer** の責任とする。

現在の実装はこの対称性を破っている: CUI (`reyn chat`) は inbox を `session.run()` で
駆動する canonical 経路だが、 MCP (`reyn mcp serve`) と A2A (`reyn web` FastAPI
router) は `session.run()` を完全に bypass し、 `ChatSession._handle_user_message` を
inline で呼んで `session.history` を直接 harvest する。 bypass は技術的理由
(= MCP SDK stdio transport が `asyncio.create_task` で spawn した background coroutine
を starve させる) があったが、 結果として:

- 新規 inbox kind (= FP-0012 の `skill_completed` 等) を追加するたびに bypass 経路にも
  parallel drain handler が必要 — 既に 2 件積み上がっている (= G27 batch 17 の
  `running_plans` + R-A2A-COMPLETION-DRAIN の `running_skills` +
  `drain_skill_completed_inbox`)。
- multi-agent relay semantics (= `_PendingChain` / `agent_request` /
  `agent_response`) は CUI inbox loop 経由でしか自然に表現できない; A2A peer は
  natively 参加できない。
- reply routing が **implicit** (= CUI は outbox を terminal print、 MCP は history
  harvest)、 message envelope に「誰に返すか」 が乗っていないので agent 本体が
  特定 transport を addressing できない。

本提案は inbox を **全 transport 共通の単一 intake channel** に格上げし、
`TransportRef` で tag された outbox を routing layer が正しい送出先に fan-out する。
`session.run()` が唯一の consumer になり、 transport 固有 code は put / route adapter
に縮退する。

---

## Motivation

### Reyn の元々の thesis

peer agent は最初から、 `agent_request` / `agent_response` という inbox message で通信し
ていて、 これは `user` message と `kind` field 以外 identical な形をしている。 router
LLM は「どの peer が話しかけてきたか」 を知らないし知る必要もない — message を処理して
reply を返すだけ。 この対称性こそが multi-agent delegation / A2A / (将来の) 外部 MCP
client が全て同じ RouterLoop + tool surface で動作することを可能にする。

agent の contract は本来こうあるべき:

```
                      ┌───────────────────────────────────────┐
                      │ Transport adapters (= I/O 専任)        │
                      │                                       │
TUI ─────────────────►│ inbox.put({kind="user",               │
MCP stdio ───────────►│            payload=...,               │──► inbox (queue)
A2A HTTP ────────────►│            reply_to=<TransportRef>})  │
peer agent ──────────►│ (= "agent_request" with reply_to =    │
                      │     相手 agent inbox)                  │
                      └───────────────────────────────────────┘
                                       │
                                       ▼
                       session.run() — 唯一の consumer
                                       │
                                       ▼
                                outbox.put(
                                  OutboxMessage(text=...,
                                                reply_to=<envelope.reply_to>))
                                       │
                                       ▼
                      ┌───────────────────────────────────────┐
                      │ Routing layer (= reply fan-out)       │
                      │ reply_to の discriminator で dispatch:  │
                      │   TUI       → renderer                │
                      │   MCP req   → JSON-RPC response       │
                      │   A2A req   → HTTP response           │
                      │   AgentRef  → peer.inbox.put(         │
                      │                 kind="agent_response")│
                      └───────────────────────────────────────┘
```

### 現実装が対称性を破っている箇所

`mcp_server.send_to_agent_impl` が `_handle_user_message` を inline 駆動するのは
「小さな shortcut」 ではなく、 **turn lifecycle 全体を fork している**:

| 軸 | CUI (`reyn chat`) | MCP / A2A (`send_to_agent_impl`) |
|---|---|---|
| inbox consumer | `session.run()` 長寿命 task | per-request inline drain |
| turn 境界 | inbox kind 1 件処理 | `send_to_agent_impl` return |
| `skill_completed` 処理 | inbox loop が natural pickup | `drain_skill_completed_inbox` で明示 drain |
| `running_plans` 完走 | natural pickup | `await asyncio.gather` で明示待機 |
| 並行性 | inbox = serialization point | per-agent `asyncio.Lock` (= 別途) |
| reply routing | implicit (outbox → TUI) | implicit (`session.history` harvest) |
| multi-agent relay | `_PendingChain` で native 動作 | A2A 入口からは不可 |

bypass は MCP SDK stdio transport の starvation 動作で正当化されていた:
`asyncio.create_task` で spawn した `session.run()` coroutine は、 request handler が
LLM call を await している間 schedule されない。 これは real constraint だが、 **fix は
inbox を bypass することではなく、 event loop を握っている同じ task で `session.run()`
を駆動すること** であるべき。

### skew が tech debt を積み上げている evidence

1. **R-A2A-COMPLETION-DRAIN** (= commit `b3252be`、 2026-05-11): FP-XXXX が新規 inbox
   kind を導入するたびに、 `send_to_agent_impl` 内に parallel drain handler が必要。
   同じ pattern は既に `running_plans` (G27 batch 17、 ADR-0023 §2.1.1) で 1 度払って
   いる; 今回が 2 度目。
2. **history harvest の脆さ**: `_new_agent_history_entries` が `chain_id` で filter
   しているのは、 同 agent への concurrent `send_to_agent_impl` call が互いの reply
   を pickup してしまう cross-talk 防止のため。 single-consumer inbox model なら
   不要。
3. **A2A peer は multi-hop chain に native 参加不可**: A2A 駆動 agent X が agent Y の
   reply を必要とする場合、 現状は「delegate spawn → history harvest」 の path しか
   ない。 統合設計なら Y の `agent_response` が X の inbox に peer agent message と
   同形で届く。
4. **test surface 重複**: `test_send_to_agent_waits_for_plan_terminal_text` /
   `test_send_to_agent_drains_skill_completed_inbox` / 将来の async-completion inbox
   kind は各々独自の bypass-path 回帰 net を要求する。

---

## Proposed implementation

### Component A — `TransportRef` discriminated union (= reply-to schema)

`src/reyn/chat/transport.py` に新規 value object:

```python
TransportRef = (
    | TuiRef()                                    # local terminal renderer
    | McpRef(request_id: str)                     # 1 つの MCP JSON-RPC request
    | A2aRef(request_id: str)                     # 1 つの FastAPI A2A request
    | AgentRef(agent_name: str, chain_id: str)    # peer agent inbox
    | SystemRef()                                 # 内部 (= skill_completed 等、
                                                  #   external sender なし)
)
```

`InboxMessage` payload に `reply_to: TransportRef` を追加 (= migration 中は optional、
post-migration で required)。 `OutboxMessage` にも routing 用 `reply_to: TransportRef` を
追加。

### Component B — `session.run_one_iteration()` (= pumping model)

`session.run()` の `while True: kind, payload = await _consume_inbox()` を **1
iteration 単位** に decompose:

```python
async def run_one_iteration(self) -> bool:
    """inbox kind を 1 件処理して return。 shutdown で False、 それ以外 True。

    handler dispatch は run() と同一; 唯一の差は while ループの有無。
    pumping するか while で回すかは caller が決める — 長寿命 session は
    永久 loop (= CUI)、 request-driven は quiescent まで pump (= MCP / A2A)。
    """
    kind, payload = await self._consume_inbox()
    if kind == "shutdown":
        return False
    if kind == "user":
        await self._handle_user_message(...)
    elif kind == "skill_completed":
        await self._handle_skill_completed(payload)
    elif kind == "agent_request":
        await self._handle_agent_request(payload)
    elif kind == "agent_response":
        await self._handle_agent_response(payload)
    return True
```

`session.run()` は trivial wrapper に縮退:

```python
async def run(self) -> None:
    while await self.run_one_iteration():
        pass
    await self._drain_on_shutdown()
```

### Component C — `RoutingLayer` (= outbox → transport fan-out)

小さな `RoutingLayer` class が `outbox` を subscribe し、 各 `OutboxMessage` を
`reply_to` の type で dispatch。 transport adapter が handler を register:

```python
class RoutingLayer:
    def register(self, ref_type: type[TransportRef], handler: Callable): ...
    async def dispatch(self, msg: OutboxMessage) -> None:
        handler = self._handlers[type(msg.reply_to)]
        await handler(msg)
```

CUI は `TuiRef → renderer.print` を register; MCP server は
`McpRef → resolve_request_future(request_id, msg.text)`; A2A router は
`A2aRef → resolve_request_future(request_id, msg.text)`; peer-agent delegate は
`AgentRef → other_session.inbox.put(...)` を register。

### Component D — `MessageBus` for request/reply correlation

MCP / A2A request handler は put-and-wait だけでは「request 完了 (= 当該 request 向け
narration が全て emit 済)」 を判定できない。 per-request correlation channel:

```python
class MessageBus:
    async def request(
        self, agent: ChatSession, kind: str, payload: dict,
        reply_to: TransportRef, *, timeout: float,
    ) -> list[OutboxMessage]:
        """`agent.inbox` に `reply_to` tag 付き message を put し、 同じ task で
        `agent.run_one_iteration()` を pump し続ける。 以下のいずれかまで継続:
        - routing layer が reply_to=<this ref> の outbox 全 drain 完了 を report
          かつ in-flight task (running_skills / running_plans / 当該 chain の
          pending_chains) が 0、 または
        - timeout fire。

        この request 向けに emit された OutboxMessage 一覧を返す。 同じ task で
        pump することで MCP SDK stdio starvation 問題を回避 (= starve される
        background task が存在しない)。
        """
```

CUI は `await session.run()` (= `while True: pump` と等価)。

MCP / A2A は post-migration でこうなる:

```python
# mcp_server.send_to_agent_impl, post-migration
async def send_to_agent_impl(registry, *, agent_name, message, timeout):
    session = registry.get_or_load(agent_name)
    bus = registry.message_bus
    req_id = _new_request_id()
    replies = await bus.request(
        session,
        kind="user",
        payload={"text": message},
        reply_to=McpRef(request_id=req_id),
        timeout=timeout,
    )
    return {"reply": "\n\n".join(r.text for r in replies), ...}
```

inline `_handle_user_message` も `running_plans` / `running_skills` gather も
`drain_skill_completed_inbox` も全て消える。 全部 bus の 「quiescent まで pump」 loop の
結果として導出される。

### Component E — Multi-agent relay が自然に表現できる

`_PendingChain` / `agent_request` / `agent_response` は simplify される: agent X の
delegate tool が agent Y の inbox に「 `reply_to=AgentRef(X, chain_id)` 付き message」 を
drop; Y が処理して `reply_to=AgentRef(X, chain_id)` 付き reply を emit; routing layer が
X の inbox に `agent_response` として配送。 `_PendingChain` book は chain-timeout
watchdog 用 metadata に縮退し、 配送本体には載らない。

---

## Open design questions (ADR に delegate)

提案が原則的に accept された後に、 follow-up ADR (複数の可能性あり) で詰めるべき
non-obvious な sub-decision:

1. **ADR-A: Starvation feasibility 検証**。 MCP request-handler task で
   `run_one_iteration()` を pump すれば pre-FP-0013 で観測した starvation が本当に
   解消するか。 migration に commit する前に stdio e2e test で empirical 検証。 pumping
   でも starve するなら、 per-request `asyncio.Task` + 明示 yield に fallback。
2. **ADR-B: `TransportRef` schema + serialization**。 ref は pure runtime object か、
   それとも crash recovery 跨ぎ (= snapshot 永続化) が必要か。 `AgentRef` は yes
   (= in-flight cross-agent chain のため)、 `McpRef` / `A2aRef` は no (= transport
   request は process と共に死ぬ)。
3. **ADR-C: Routing layer と outbox の lifecycle**。 現在 `outbox` は per-session
   asyncio.Queue で CUI renderer が consume する。 post-migration で routing layer は
   per-registry singleton か per-session か、 また外部 reply_to を持たない slash
   command echo (= `/skill list` / `/tasks` 出力等) との interaction はどう設計するか。
4. **ADR-D: Migration ordering**。 `run_one_iteration` を既存 `run()` と coexist 可能な
   形で ship できるか (= 両方 available、 transport が選ぶ)、 それとも bypass を
   lockstep で削除して 2 path drift を防ぐべきか。
5. **ADR-E: `MessageBus.request` の quiescence 判定**。 「当該 reply_to 向け reply 全
   drain 済」 は straightforward; 「当該 chain の in-flight task が 0」 は
   `running_skills` / `running_plans` / `pending_chains` を chain_id で filter する
   厳密な predicate が必要。 cross-chain 干渉 (= 同 agent への concurrent request) で
   quiescence が誤判定されてはいけない。
6. **ADR-F: 短期 patch の backward compat**。 FP-0013 land 時に tactical
   `drain_skill_completed_inbox` を削除するか、 `MessageBus` 不在時の fallback として
   残すか。 default plan: bypass 削除と同 commit で削除。

---

## Dependencies

- **FP-0012 (LANDED 2026-05-10)** — skew を surface させた最初の async inbox kind
  `skill_completed` を提供。 FP-0012 前は bypass が accidentally 機能していた。
- **R-A2A-COMPLETION-DRAIN (LANDED 2026-05-11、 commit `b3252be`)** — tactical
  patch; FP-0013 が obsolete 化し、 migration 中に削除。
- **PR21 (LANDED)** — `run_one_iteration` が継続 honor すべき inbox WAL semantics。
  schema 変更は予定なし。
- **ADR-0023 (LANDED)** — plan-mode async dispatch; `send_to_agent_impl` 内の
  `running_plans` await pattern (G27 batch 17) は本提案が subsume する 2 件目の
  tactical patch。

新規外部 dependency なし。

---

## Migration plan (high-level)

1. `TransportRef` schema を land (additive、 behavioural 変化なし)。
2. `run_one_iteration` を `run()` と coexist させて land (= refactoring、 両方 green)。
3. `RoutingLayer` を land + 既存 renderer 相当の `TuiRef` handler 登録。
4. `MessageBus.request` を land、 MCP/A2A 用の `request_inline_pump` mode を実装。
5. **Feasibility checkpoint**: MCP stdio e2e で pumping model 下でも starve しないか
   検証。 ここで block — pumping でも starve するなら ADR-A resolution を待つ。
6. `send_to_agent_impl` を `MessageBus.request` に migrate (= bypass 削除)。
7. multi-agent relay を `AgentRef` `reply_to` に migrate (= `_PendingChain` は
   chain-timeout 責任のみ保持)。
8. Tactical patch 削除: `drain_skill_completed_inbox` /
   `_new_agent_history_entries` の chain_id filter / per-agent `asyncio.Lock`。
9. Test coverage: 全 transport surface で同 set の contract test (= 1 round-trip +
   完了 narration + multi-hop delegation) を parameterize、 `TransportRef` 全 variant
   で。

---

## Cost estimate

**LARGE** (~1-2 週の集中作業、 starvation feasibility 結果に依存)。

内訳:

- TransportRef schema + test: ~0.5 day
- `run_one_iteration` decomposition + behavioural test: ~1-1.5 day
  (`_drain_on_shutdown` interaction に注意)
- RoutingLayer + TUI adapter: ~1 day
- MessageBus + quiescence predicate: ~1.5-2 day (= 最も subtle な箇所)
- Starvation feasibility verification: ~0.5-1 day (= pumping mode の redesign を
  強いる可能性)
- MCP + A2A migration: ~1 day
- Multi-agent relay migration: ~1-1.5 day
- Tactical patch 削除 + symmetric test coverage: ~0.5-1 day
- ADR drafting (A-F 必要なもの): ~0.5-1 day

並列 sonnet で schema / TUI adapter / migration step は短縮可能。 quiescence
predicate (`MessageBus.request`) は parallelise しにくい — おそらく critical path。

---

## Risks

- **Pumping model でも starvation が残る** — fallback plan: per-request
  `asyncio.Task` で既知の await point に明示 `asyncio.sleep(0)` yield を inject。
  抽象性は劣るが skew は維持可能。
- **Quiescence 誤判定** — `MessageBus.request` が predicate 緩過ぎで narration emit
  前に return する可能性。 緩和策: 保守的 predicate + 各 async inbox kind の
  integration test matrix。
- **CUI 挙動 regression** — `run_one_iteration` は現 `run()` loop の全 edge case
  (shutdown signal / exception handling / `_drain_on_shutdown` invariant) を保たねば
  ならない。 緩和策: `run()` を trivial loop wrapper として保持、 behavioural test で
  pin。
- **pre-existing chitchat replay flake** (= R-A2A-COMPLETION-DRAIN 検証中に観測) は
  本 work と independent。 blocker ではないが、 regression net の信頼性確保のため
  migration 前に修正しておく価値あり。

---

## Related

- **FP-0012**: async skill execution — `skill_completed` で skew を surface させた。
- **R-A2A-COMPLETION-DRAIN**: commit `b3252be`、 本提案が obsolete 化する tactical
  patch。
- **ADR-0023**: plan-mode async dispatch — 「 bypass path に明示 await」 pattern の
  最初の事例。
- **`docs/concepts/async-skill-execution.md`** — 現在の architecture doc; FP-0013 land
  時に rewrite。
- **G27 batch 17 (= commit `3a59d8c`)**: `send_to_agent_impl` の `running_plans`
  gather patch は本 surface area で 2 件目の tactical workaround; FP-0013 が subsume。
