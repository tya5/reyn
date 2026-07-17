---
type: concept
topic: runtime
audience: [human, agent]
search_hints: [intervention delivery, ask_user, permission prompt, intervention bus, BridgeToParent, spawn bridge, pipeline driver, originator, answering surface, fail-close, intervention orphan, stalled intervention, present routing]
---

# Intervention delivery

An **intervention** is any moment where the OS must pause an in-flight operation and get an
answer from a human: an `ask_user` question, a just-in-time permission prompt, an MCP
elicitation, a `safety.limit` "keep going?" gate. The work that raises an intervention is
often **not running where the operator is looking** — it may be a spawned pipeline driver, an
agent-step worker, or a deep fan-out branch. Intervention delivery is the guarantee that the
question still reaches the person who can answer it, and that when nobody can, the run does not
hang.

## The rule

> **Whichever component (step) raises it, an intervention — and a `present` — is answered by
> the originator the run is ultimately attached to. When there is no attached originator, it is
> closed and answered (fail-close).**

Two clauses, no per-step branch. There is deliberately no "…except for a tool step" or "…MCP
calls are special". A feature that needs its own delivery path is the failure sign (see below).

## The resolution is existing and correct — the fix is uniform threading

The hard part — *which* surface answers — is already solved and is **not** re-invented per
call site. A spawned session carries a typed **intervention bridge**
(`runtime/session_buses.py`):

- **`SpawnBridgeInterventionListener`** (an *attached* spawn — a pipeline driver spawned
  `BridgeToParent`, an agent-step worker under an attached invoker) resolves *compositionally
  toward the outermost attached originator*. It walks parent → parent, so a grandchild's prompt
  reaches the human via the first ancestor that actually serves an operator, never pinning on an
  intermediate headless session.
- **`AuditOnlyInterventionBridge`** (a *detached* / headless spawn — `start_pipeline_run`, a
  `reyn pipe` run, a worker with no live invoker) resolves every intervention to a typed,
  reason'd **refusal** that returns immediately — the fail-close clause, by construction, at
  every depth. Never an unbounded park.

Both clauses of the rule are therefore *properties of the bridge chain*, decided once at spawn
time. The only thing an individual op does is **build its intervention bus from that chain**
rather than binding it to its own session. When every IV-raising leaf does that uniformly, the
rule holds with no branching.

### The single construction seam

For router-initiated ops, that bus is built in exactly one place —
`Session._make_router_intervention_bus()`:

- **bridge present** (this session is an attached driver/worker) → dispatch on the parent's
  live-operator listener (the chain above);
- **no bridge** (a root chat, or a detached session whose bridge is `AuditOnly`) → a self-bound
  `ChatInterventionBus` on this session's own registry.

`RouterHostAdapter`'s intervention-bus factory and every MCP op method (`_mcp_call_tool` and its
resource/prompt siblings) route through this one seam. `ask_user` and `present` already reached
the originator because they, too, ride the spawn-time bridge (`present` via the analogous
`SpawnBridgePresentationConsumer`) — the fix brought the MCP leaf into the same discipline.

## The failure that motivated the rule (#3049)

A chat-invoked `rag_ingest` pipeline hung indefinitely. Its X1 pre-flight fanned out
`call_mcp_tool` reachability probes; each probe's permission gate raised a `permission.generic`
intervention. The driver session's MCP op methods **hardcoded a self-bound
`ChatInterventionBus(self, …)`**, bypassing the bridge. So on an *attached* driver — where a live
operator was blocked on the parent, ready to answer — the prompt landed on the *driver's own*
listener-less registry: dispatched, parked stalled, and awaited forever. (Live-process
measurement id-matched all 18 orphaned branch futures 1:1 to the stalled `permission.generic`
interventions.)

The self-bound bus was *correct* for a root chat and *wrong* for a driver — a single hardcoded
construction cannot tell the two apart. Routing it through the bridge-aware seam fixes both
uniformly: the attached driver reaches the operator, the detached one fails closed.

## The failure sign

> If an implementation needs a **different way to deliver** for one step kind or one op — a
> bespoke bus, a special-cased routing branch — that is fragmentation leaking back into the
> rule.

The correct design has one resolution (the bridge chain) and one construction seam; a leaf only
chooses to *use* it. When a new IV-raising router-op seam is added, it inherits origin-delivery
for free by building its bus through `_make_router_intervention_bus`. A seam that instead
constructs its own self-bound bus reintroduces the #3049 orphan for any attached spawn — the
structural guard in `tests/test_3049_driver_router_op_intervention_reaches_originator.py`
fails on exactly that.

## Not yet uniform: the limit-policy bus

The per-LLM-call `safety.limit` gates (`cost.*`, `timeout.llm_call`) are dispatched through a
**separate** bus wiring (`_ChatBudgetBus` → `Session._dispatch_intervention`), not the router-op
seam above. For a spawned session this bus is *also* not bridge-aware — a `safety.limit` prompt
there does not reach the originator (it auto-refuses locally rather than hanging). This is the
same fix-class as #3049 but a distinct seam and a distinct symptom; it was not the measured RAG
hang and is tracked separately. The rule stated above is the target for that seam too.

## See also

- [Permission model](permission-model.md) — the gates that raise `permission.*` interventions.
- [Present layer](present.md) — `present` rides the same spawn-time surface routing.
- [Safety](safety.md) — the `on_limit` policy behind the `safety.limit` gate.
- [Pipelines](pipelines.md) — attached vs detached pipeline runs (the driver spawn modes).
