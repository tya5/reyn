# FP-0048: Gateway outbound — self-contained inbound+outbound plugins

**Status**: proposed (design-review only — no implementation)
**Proposed**: 2026-06-19
**Author**: e2e-coder session (#1805)
**Gate**: feature, not a pure move. Lead review → (if the commitment is large)
owner review → implement. No code is written by this doc.

> Line numbers are as-of `origin/main` post-#1816 (the `reyn.plugins` →
> `reyn.gateway` rename). Method / section names are the authoritative anchors.

## Problem (#1805)

Reyn's interaction architecture is the industry-standard pair — **inbound =
webhook** (HTTP), **outbound = MCP tool** — and Reyn core stays
platform-agnostic (every platform outbounds through the same MCP mouth via
`external_routing.route_to_mcp`). That design is sound and is **not** what
changes here.

The gap: the sample gateway plugins (`reyn.gateway.sample_slack` /
`sample_line`) implement **inbound only**. For outbound, the operator must
**separately run a platform MCP server** (e.g. a Slack MCP server) and point
`external_transports` at it. If that separate process dies, the agent's replies
**vanish silently** (logged, never delivered, no user-facing signal). A
*complete* gateway plugin should provide **both** inbound and outbound in one
self-contained unit — the way Hermes' single-file `telegram.py` does inbound
(webhook) + outbound (sendMessage).

## Recommended design (案B + 案C, lead-endorsed)

> **Implementation update (flow-trace before the cut, #1805 — lead-endorsed):
> 案C is DROPPED, 案B is the whole feature.** Tracing the outbound path during
> implementation showed the **agent's outbound is already complete**:
> `deps.py` wires `make_outbox_interceptor(routing, mcp_dispatcher=
> make_session_mcp_dispatcher(session))` → `route_to_mcp` → `mcp_handle`. The
> 案B in-process tool **reuses that existing path** to close crash-vanish — the
> agent reply → outbox → interceptor → `route_to_mcp` → in-process tool flows
> with no new code. So `send_to_transport` (案C) would only **duplicate** the
> existing outbound, its issue-signature `send_to_transport(transport,
> destination, text)` can't be built cleanly (it needs `session` for
> routing+dispatcher), and its caller is ambiguous (the agent uses the
> interceptor; the plugin's tool handler does the Bot-API call). 案C = YAGNI,
> **deferred**. This doc's remaining 案C subsections (the `gateway.api`
> helper, open question 2) are superseded by this note — same "shrink the
> design correctly via pre-impl flow-trace" discipline as the C6 9→6 and C7
> dead-vs-live cuts.

**案B (spine): the plugin's outbound tool is hosted in-process by `reyn web`,
not a separate process.** ~~案C (authoring surface): a `gateway.api` outbound
helper~~ — **dropped (see the update note above); the existing
`route_to_mcp` path already serves it.**

### Why B over A / C-alone

- **案A** (plugin embeds its own MCP server) — heavier, and it is *still* a
  separate server with its own lifecycle. Rejected.
- **案C alone** (just a `gateway.api` helper wrapping `route_to_mcp`) — does not
  fix crash-vanish if `route_to_mcp` still targets an *external* MCP server.
  Rejected as the spine; kept as the authoring surface on top of B.
- **案B** — in-process ⇒ no separate process to crash ⇒ crash-vanish dissolved
  structurally; reuses infrastructure that **already exists** (below).

### The in-process mount leverages existing infra

`reyn web` **already hosts an MCP server over SSE**: `web/server.py` mounts
`web/routers/mcp.py` (`app.include_router(_mcp_router.router)` +
`get_mcp_message_mount()`, `/mcp/sse` + `/mcp/messages`), backed by the reyn
MCP server (`reyn.mcp.server`, which already exposes e.g. `send_to_agent`). So
the outbound tool does **not** need a new server — it needs to be **registered
into the MCP tool set `reyn web` already serves**.

### Self-loop wiring (make this explicit — lead's point 1)

```
            ┌─────────────────────── reyn web (one process) ───────────────────────┐
  platform  │  POST /webhook/slack ─▶ gateway.api.push_to_agent ─▶ agent inbox      │
  ───────▶  │                                                          │            │
            │                                                          ▼ (reply)    │
  ◀───────  │  slack__send  ◀── /mcp/sse ◀── route_to_mcp ◀── agent outbound        │
            │  (gateway outbound tool, in-process)                                  │
            └───────────────────────────────────────────────────────────────────────┘
  external_transports.slack → mcp tool "slack__send" on localhost reyn-web /mcp
```

One `reyn web` process hosts **both** the inbound webhook route (mounted via
`plugin_loader.load_webhook_plugins`) and the outbound MCP tool (registered into
the in-process MCP server). `external_transports` points the `slack` transport
at the **local** reyn-web MCP tool — a self-loop, no second process.

### Relationship to `route_to_mcp` (lead's point 3)

**No new outbound path.** The agent still outbounds via
`external_routing.route_to_mcp(transport="slack", …, mcp_dispatcher=…)`, which
resolves `slack__send` through the injected `mcp_dispatcher` (`<server>__<tool>`,
per `web/deps.py`). 案B only changes **where that MCP tool lives** — in the
reyn-web process instead of a separate server. The dependency direction is
unchanged: agent → `route_to_mcp` → MCP tool; the tool now happens to be
co-hosted.

### `gateway.api` outbound helper (lead's point 2)

Symmetric to the inbound `push_to_agent(*, target_agent, text, sender,
reply_to=…, kind=…, …)`. Proposed:

```python
async def send_to_transport(
    *,
    transport: str,             # "slack" / "line" — keys external_transports
    destination: dict,          # opaque per-transport routing (channel/user/…)
    text: str,
    media: list[dict] | None = None,
    registry: Any | None = None,
) -> RouteResult:
    """Outbound counterpart to push_to_agent — deliver `text` to `transport`'s
    configured MCP tool. Wraps external_routing.route_to_mcp so the plugin
    author never constructs MCP tool names / dispatchers by hand."""
```

The plugin's outbound MCP tool handler is then a thin call into the platform's
Bot API (`chat.postMessage` / LINE push); `send_to_transport` is what *agent-
side* callers (and tests) use, while the tool itself is what the agent invokes.
(Open question 2 below: which of these the helper actually fronts.)

## The key design decision (for review): tool-registration mechanism

How does a gateway plugin get its outbound tool into the MCP tool set `reyn web`
serves? Two candidate mechanisms:

- **(i) Plugin hook into `reyn.mcp.server`'s tool set** — `load_webhook_plugins`
  (or a sibling) calls a plugin-supplied `register_tools()` that adds
  `slack__send` to the in-process MCP server's catalog. One MCP endpoint, plugin
  tools merged in. *Cleaner for the agent (one transport target), needs an
  extension point on the MCP server.*
- **(ii) Per-plugin MCP sub-endpoint** — the plugin mounts its own
  `/mcp/<name>` router (mirrors how it already mounts its webhook router);
  `external_transports` targets that sub-path. *No change to `reyn.mcp.server`,
  but N endpoints + N transport entries.*

**Lean: (i)** — one in-process MCP server with plugin tools merged keeps the
agent's `external_transports` simple and matches "one mouth (MCP)". Confirm in
review; this is the load-bearing mechanism.

## Backward compatibility (lead's point 5)

**Unchanged for existing operators.** `external_transports` can still point at
an **external** MCP server — the in-process self-loop is an **opt-in** for
self-contained plugins, selected purely by what `external_transports.<t>`
targets (local reyn-web vs remote). No migration; `route_to_mcp` is untouched.

## Gate (lead's point 4 — feature, not pure-move)

- **New outbound, unit-tested**: `send_to_transport` resolves the transport →
  tool, handles `status="unconfigured"`, surfaces the Bot-API failure (the
  crash-vanish fix means a *failed* send is now a surfaced `RouteResult`, not a
  silent drop).
- **E2E round-trip**: webhook POST → `push_to_agent` → agent turn → outbound
  `route_to_mcp` → in-process `slack__send` → (faked Bot API) — asserting the
  reply reaches the platform sender in one process.
- **Inbound unchanged**: the existing `tests/gateway/` inbound suites + replay
  stay green (this is additive; the webhook path is untouched).
- **Crash-surface test**: when the outbound tool's Bot-API call fails, the
  failure is reported (not silently logged) — the regression the issue is about.

## Open questions for review

1. **Tool-registration mechanism (i) vs (ii)** — lean (i) (MCP-server tool hook).
2. **Helper fronting**: does `send_to_transport` front (a) the agent→tool
   `route_to_mcp` call, or (b) the tool→Bot-API send? Likely (a) is the
   `gateway.api` surface; (b) is the plugin's tool handler. Confirm naming.
3. **Scope of the first cut**: ship outbound for `sample_slack` only (prove the
   mechanism end-to-end), then `sample_line` as a fast-follow? Or both together.
4. **PLUGIN_GUIDE.md** update is in scope (authoring a complete inbound+outbound
   plugin) — same PR or docs-maintainer follow-up.

## Related

- #1805 (this gap) · FP-0041 #489 (`push_to_agent` / `route_to_mcp` origin)
- #1812 (the gateway concept doc this completes) · #1807 (the gateway rename)
- #1800 (agent lifecycle hooks — adjacent, separate)
