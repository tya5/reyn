# ADR-0039: N thin CUI clients × one single-writer server — UI-path unification, four-surface separation

**Status**: Proposed (owner BUILD decision obtained; phased P0–P6).
**Track**: Runtime interaction model — the concretization of ADR-0018's deferral
("multi-process inside one workspace is not a goal; cross-process / cross-Reyn
is a future layer's job") and a successor seam to ADR-0001 (single-process WAL
+ snapshot state model).

---

## Context

Reyn is single-process by design: concurrent agent sessions run as asyncio
tasks in one process, sharing one file workspace; the audit-event log
(`.reyn/events`, P6) + WAL are the durable recovery/replay source of truth.
ADR-0018 deferred any cross-process story to "a future layer."

This ADR ratifies that layer as **N thin CUI clients attached to one
single-writer server process**, and — importantly — **unifies local and
remote onto the same client**, so `reyn chat` is always a stream-consuming
client differing only by transport.

**The design thesis (the spine of every decision below):** the industry norm
for safe multi-client agents is *isolation* — a container / git-worktree / VM
per client (verified across 11 tools: coding agents isolate, frameworks are
single-process-async; **no** tool implements cross-process shared-filesystem
locking). Choosing a **shared single-writer workspace** deliberately forgoes
the safety isolation grants *for free*. We **earn that safety back by
construction** through three mechanisms — **authentication**, **fail-close**,
and **per-path locking**. An unauthenticated, non-fail-closing, or unlocked
shared-writer model would be strictly worse than isolation; the earned-by-
construction trio is what makes "shared" defensible.

---

## Relationship to prior direction — supersedes the "remote TUI deferred" positioning

Prior positioning (`docs/deep-dives/research/positioning/web-ui-direction.md`,
update 4) **deferred the remote TUI/CUI client** (the same TUI codebase
connecting to a remote server) out of scope, substituting **browser remote
access**, and framed `reyn chat` (local + embedded) and `reyn serve` (browser
remote) as the only two commands. It deferred on two grounds: (i) a
**workspace-location-semantics** concern, and (ii) a **single-user
assumption** for `reyn serve`.

**This ADR revisits and supersedes that deferral.** Under the shared
single-writer server (D2/D3), the remote CUI is revived as **`reyn chat
--connect` — a co-equal AG-UI surface alongside the browser**: both are
AG-UI clients of *one* server, not two separate command families. The two
grounds are resolved, not ignored:
- **(i)** The workspace-location-semantics concern is **recast as an
  intended property** (D3's irreducible workspace-affordance gap): a remote
  client's `file:line` / `!`-shell locality points at the *server's*
  workspace *by design*, because there is exactly one shared workspace. What
  read as a blocker under an isolation assumption is a defined semantic under
  the shared-writer model.
- **(ii)** The single-user assumption is **the v1 default with a defined
  extension seam**: per-user-ID authorization (D5(a), Axis A) is where
  multi-user secret/session isolation + audit attribution land — scoped, not
  left as a contradictory standing position.

The positioning doc's "Deferred: remote TUI client" section is thereby
superseded; `docs/deep-dives/research/positioning/web-ui-direction.md` is
updated in the same PR that lands this ADR to point here.

---

## The live seams we extend (flow-trace)

- **Session driving** — `runtime/session.py::run_one_iteration`: already
  splits "long-lived loop (CUI)" from "request-driven pump (server)".
- **Outbox streaming** — `interfaces/web/ws/chat.py`: N-client fan-out of
  `OutboxMessage`→JSON, detach-on-disconnect, drain-continues-in-background.
- **Dual render stream** — `interfaces/repl/renderer.py`: the renderer
  consumes **both** `message(OutboxMessage)` and `on_chat_event(event)`; the
  "Working…" / WaitingOn indicator is driven by the chat-event stream, not the
  outbox.
- **Network intervention** — `interfaces/web/a2a_intervention.py`: the
  two-way pause already works over the wire (peer learns "input-required"
  before the awaiter blocks).
- **Fail-close terminal** — `runtime/session_buses.py::AuditOnlyInterventionBridge`:
  a run with no attachable operator surface gets a typed, reason'd DENY for
  `ask_user` — never an unbounded park.
- **Present render model** — `core/present/binding.py::ResolvedPresentation.nodes`:
  a plain `list[dict]`, **neutralized at construction** (the single
  leaf-neutralization seam), i.e. inert before it reaches any renderer or wire.
- **Per-path write lock** — the single-writer's concurrent sessions are
  serialized on a resolved-path-keyed `asyncio.Lock` (the write-safety
  mechanism ratified alongside this arc; same in-process family as the WAL
  serialization lock).

---

## Decision

### D1. Four-surface separation
Distinct protocol boundaries, never conflated: **MCP = tools · A2A =
agent↔external-agent · AG-UI = agent↔UI interaction · OTEL = observability
(export-only)**. The client speaks **AG-UI only** — it is a UI, not an agent.
Reyn's A2A implementation is reused as an **internal spine** (session
driving, network intervention pause); the client never speaks A2A. OTEL is
not a client at all (D7).

### D2. UI-path unification — one stream-consuming client, transport-pluggable
The inline CUI already dispatches on `OutboxMessage.kind`. Make that the
**only** client and abstract the stream **source** behind a transport seam:
**in-process transport** (local) vs **AG-UI/SSE transport** (remote). Both
feed the **same renderer** ⇒ draw / input / intervention / status-bar are
**bit-identical**. The seam must carry **outbox + the renderer-relevant
chat-event subset** (WaitingOn is chat-event-driven; an outbox-only wire
would drop it and break bit-identity). Local ≡ remote by construction; the
thin-client and single-writer properties fall out for free.

### D3. Single-writer server + N thin clients
The server process is the **sole writer** of Session / LLM / tool / state /
`base_dir`. Clients **attach** (not own) and are **pure I/O** (render +
input). The only **irreducible** local-vs-remote differences are **(1)
latency** (physical; minimize only) and **(2) workspace-location affordance**
(`file:line` and `!`-shell locality point at the server's files) — both are
the shared-workspace model behaving as intended. Everything else is
identical.

### D4. Client↔session multiplicity + two orthogonal axes (identity/authz vs active-driver/seize)
The server hosts **N sessions**; a client attaches to a session (does not own
it); all sessions share the single-writer workspace. **N client ↔ N session**
is the base case. Shared-session (**2-on-1**) is governed by **two orthogonal
axes** that must not be conflated:

- **Axis A — identity + authorization = *security*** (detailed in D5(a) /
  P0): who a connection is (a user-ID) and what that identity may do. Answer
  authority, seize eligibility, and fencing all derive from Axis A.
- **Axis B — active-driver / seize = *UX coordination*** (this decision):
  which *connection* currently holds interactive authority. Per-connection,
  **transport-independent**, and — crucially — **not a security control**.

**Axis B (the 2-on-1 UX), primary use case = one user across multiple
terminals** (laptop + desktop), not collaborators:
- **Attached connections are equal peers.** No primary/owner/privileged
  connection. The active-driver token merely marks *"which connection holds
  authority right now"* — the **location of authority, not a status
  difference**.
- **Render** fans out to all peers (equal observation) — free from the
  existing outbox fan-out. Only *authority* is the movable token.
- **Authority is a single token, held by exactly one connection at a time.**
  An *idle* terminal must not retain answer authority.
- **Symmetric seize / takeover (v1 scope, not deferred):** **any connection
  in the Axis-A-authorized set may seize equally** — no preferred connection,
  no approval handshake. For one user across terminals (same user-ID) this is
  a benign direct grab (pure UX). The prior holder returns to a non-holding
  equal peer (notified; may re-seize).
- **"Equal" is about connection *status*, not simultaneous driving.** At any
  instant exactly one connection is active; any authorized connection can
  take over. **Simultaneous full-collaborative** driving (multiple concurrent
  drivers with input serialization) is a **later phase**; this symmetric
  single-token model is its substrate.
- **Security lives in Axis A, not here.** Seize being "symmetric among the
  authorized set" is a *UX* statement; the *security* invariants — an
  unauthenticated connection can neither answer nor seize, and grant
  authority is identity-gated — are Axis A (D5(a)). Audit stamps **both** the
  identity (Axis A attribution) and the connection (Axis B — which
  terminal).

### D5. Safety earned by construction (the thesis, made concrete)
Three mechanisms replace isolation's free safety:
- **(a) Authentication + identity/authorization (Axis A; P0, gates the
  build).** A network intervention answer *is* a permission grant. **A
  connection carries an identity (a user-ID); the identity carries two
  ORTHOGONAL attributes** that must not be conflated:
  - **Identity class ⇒ fencing (`external_source`) — injection defense.**
    Human-operator class ⇒ unfenced (`False`); **agent-peer class (A2A) ⇒
    fenced (`True`)** — an agent peer is an injection vector, a human is not.
    Fencing tracks **class, never privilege level** (a low-privileged
    *human*'s answer is still human input; the fence is not the control for
    privilege).
  - **Per-user-ID authorization scope — privilege control.** What the
    identity may do: which intervention kinds it may answer (e.g. `ask_user`
    yes, `permission.file_write` no), which sessions it may attach/seize.
    **This scope — not fencing — is the multi-user extension point.**
  In v1 there is a **single user-ID ≡ the operator** (human class + full
  scope ⇒ `external_source=False`); multi-user adds user-IDs differentiated
  by **authz scope** (all human-class, unfenced) — an authz-table extension,
  not a re-architecture. Answer authority and seize eligibility are
  identity-gated (an unauthenticated connection can neither).
  **Two-axis × three-tier, secure-by-default.** Identity is *established per
  transport tier*, but the downstream user-ID authorization is *unified*:
  - **Tier 1 — in-process** (local, same process): no auth; it is the
    operator's process.
  - **Tier 2 — same-machine**: **UDS `0600` default** (OS identity via the
    per-OS peer-credential mechanism — `SO_PEERCRED` on Linux,
    `getpeereid`/`LOCAL_PEERCRED` on macOS), TCP-loopback opt-in. The
    **browser surface** (openui) cannot use a UDS → it stays **loopback TCP +
    a startup-issued token** (Jupyter-style); "UDS default" is the
    thin-client connect surface, not every Tier 2 surface.
  - **Tier 3 — cross-machine**: **token + TLS, opt-in, fail-closed** (a
    non-loopback bind with no token ⇒ refuse to start).
  - **One server binds Tier 2 + Tier 3 simultaneously** (same-machine always
    on; network opt-in).
  The connect-surface auth spec details the mechanism; this ADR records the
  ruling.
- **(b) Unified fail-close.** A pending intervention whose **last attached
  surface is lost** — in-process detach OR network break / heartbeat timeout
  — resolves via the typed-DENY terminal, never an unbounded park. DENY only
  when **no** surface remains. A liveness signal is required so a half-open
  connection cannot hide the loss. This is the **load-bearing safety
  invariant** of the arc.
- **(c) Per-path write lock.** Because all writers live in one process, an
  in-process resolved-path-keyed lock serializes concurrent sessions' file
  mutations — no cross-process lock is needed (and none is industry-standard).
  The single-writer model and this lock are mutually reinforcing: the model
  makes cross-process locking unnecessary; the lock makes the shared
  workspace safe.

### D6. Protocol strategy — standard envelope, reyn-private richness
Speak **AG-UI** (mature, HTTP+SSE, ~1:1 event mapping) as the base; carry
reyn's typed-rich surfaces (structured render-node, the typed intervention
taxonomy — ask_user / permission / choice / safety-limit — audit-event refs,
rewind, present-offload) as **`Custom` extension events**. This is
isomorphic to reyn's MCP posture (standard wire + rich internal ops). Reyn's
rich payload is inevitably reyn-specific on *any* protocol, so the optimum is
neither fully proprietary nor fully standard: an interoperable core (text /
tool / status / HITL) that a generic AG-UI client consumes and degrades
gracefully, with rich payload as reyn-private semantics. **Forward-compat is
self-asserted:** AG-UI has no normative "ignore-unknown" clause, so reyn owns
it via an extension **profile + conformance test** (bounded-by-construction).
Render-node vocabulary rides `Custom` now; formalizing it as an **A2UI
(Google) catalog** — which spec-guarantees capability-negotiation +
graceful-degradation — is a future phase gated on that spec stabilizing.

### D7. Observability — OTEL export-only, durable store unchanged
OTEL is an **optional outward exporter**, not a channel or store: reyn emits
spans/metrics/logs; consumption is the operator's OTEL backend. **The
durable `.reyn/events` (P6) + WAL remain the recovery/replay source of
truth, unchanged** — OTEL is fire-and-forget / lossy and must never become
the recovery substrate (this preserves the recovery-source-survives-
truncation gate). Mapping follows GenAI semantic conventions (agent turn →
span, tool/LLM → child span, cost/token → metric, audit-event → log); those
conventions are at "Development" stability, so the mapping is versioned.
Near-term unnecessary → late phase.

### D8. Phasing (each phase independently landable; every phase's Test plan carries a reachability + fail-close assert)
- **P0 — Authentication + schema (gates all remote).** Two-axis × three-tier
  auth (Tier 2 UDS + Tier 3 token/TLS, simultaneous bind, fail-closed);
  identity-carry + user-ID authz; the `external_source`-per-identity ruling;
  seize as Axis-B UX gated by Axis-A membership; retrofit of the existing
  ws/A2A surface.
- **P1 — CUI → stream-consuming client (in-process transport).** Local `reyn
  chat` consumes outbox + chat-events *through* the seam. Assert
  bit-identical pre/post.
- **P2 — AG-UI transport + event mapping (server→client).** Mapping over
  HTTP+SSE; present-on-wire; per-connection re-guard; session-state view via
  STATE_*; snapshot-on-connect.
- **P3 — HITL + unified fail-close (load-bearing).** Typed interventions over
  frontend-tool→result→resume; last-surface-gone → typed-DENY + liveness;
  input-side command classification (client-local vs server vs
  permission-gated); attach / seize / answer attribution audit-events.
- **P4 — reyn `Custom` extension profile.** Rich payload on `Custom`;
  ignore-unknown conformance test; single-writer-by-construction assert
  (client owns no workspace/tool).
- **P5 (future) — OTEL exporter.** Per D7.
- **P6 (future) — A2UI catalog formalization + protocol consolidation** (fold
  the existing web UI onto AG-UI; retire the ad-hoc ws JSON).

---

## Positioning (proven-halves synthesis)

The design is a synthesis of two independently-proven patterns, with no
exact-match precedent:
- **Agent ↔ N-client interaction:** proven by AG-UI (CopilotKit).
- **Server-owns-workspace ↔ N-client:** proven by Jupyter's shared-kernel +
  real-time-collaboration, LSP, and code-server.

No surveyed tool combines them for an **autonomous coding agent** sharing one
workspace across clients; **shared-workspace-for-agent** is the distinctive
synthesis. (And since no tool locks a workspace across processes — all
isolate or run single-process-async — the single-writer choice is
industry-aligned, not exotic.)

Read from the client side rather than the server side, the same design is a
**session multiplexer** — the same shape as a terminal multiplexer (tmux) or
Jupyter's shared kernel, applied to an agent runtime: one server multiplexes N
agent sessions across M attached client surfaces (attach/detach, broadcast via
`OutboxHub`, seize). Single-writer and multiplexer are two faces of one
design, not two designs — "single-writer" names the server-side property,
"multiplexer" names the client-side experience it produces. Multiplexing
itself is not new (tmux, Jupyter, LSP all do it); what's new here is applying
it to a **typed, permissioned, auditable** agent runtime rather than a plain
shell or kernel process — the multiplexed unit is a governed runtime, attach
works identically local or over the network, and any standard AG-UI client can
attach, not just reyn's own.

---

## Consequences / honest ceiling

- **Rich surface is reyn-private on a standard pipe** — interoperable core,
  private richness. Owner-accepted as the intended interop level.
- **Same-machine auth = UDS `0600` (default), loopback opt-in.** UDS with
  the OS peer-credential (per-OS: `SO_PEERCRED` / `getpeereid`) is
  operator-exclusive on multi-user hosts (unlike TCP-loopback, which any
  local user can reach); it needs no secret. Loopback remains an opt-in for
  single-user simplicity. The **browser surface stays loopback TCP + startup
  token** (a browser cannot open a UDS).
- **Protocol proliferation:** AG-UI is another wire format alongside the
  ad-hoc ws JSON, chainlit, and A2A; the consolidation phase (P6) retires the
  ad-hoc one to bound the cost.
- **Bit-identical local/remote** raises the bar on the transport seam: any
  renderer input not carried over the seam is a remote regression, so the
  seam's stream coverage is a standing invariant.

---

## Alternatives considered

- **Per-client isolated workspace (industry norm).** Rejected: the owner's
  goal is a *shared* workspace (agents/operators collaborate on one codebase
  across terminals); isolation defeats the purpose. The cost — earning
  safety by construction — is accepted (D5).
- **Fully proprietary protocol.** Rejected: abandons ecosystem interop for
  richness that would be reyn-private regardless; the standard-envelope +
  private-payload optimum (D6) keeps both.
- **Cross-process file locking (flock everywhere).** Rejected: the
  single-writer model makes it unnecessary, no surveyed tool does it, and it
  is the wrong layer (an in-process lock still needed underneath).
  Cross-process remains a future holistic concern, not this arc's.
- **OTEL as the event store.** Rejected: lossy; would break the recovery
  gate. OTEL stays an additive exporter (D7).
- **A separate `--connect` client codepath.** Rejected in favor of UI-path
  unification (D2): a second codepath would drift from local; one
  transport-pluggable client keeps local ≡ remote by construction.

---

## References
- ADR-0001 (WAL + snapshot state model) / ADR-0018 (cross-process deferral)
- Supersedes: `docs/deep-dives/research/positioning/web-ui-direction.md`
  (update 4's "Deferred: remote TUI client" section)
- Live seams: `runtime/session.py`, `interfaces/web/ws/chat.py`,
  `interfaces/repl/renderer.py`, `interfaces/web/a2a_intervention.py`,
  `runtime/session_buses.py`, `core/present/binding.py`
- Competitor grounding: AG-UI (CopilotKit) for agent↔N-client interaction;
  Jupyter shared-kernel + real-time-collaboration, LSP, and code-server for
  server-owns-workspace ↔ N-client
