# 0065 — Orchestration foundation (オーケストレーション基盤整備): external-event plugins as a first-class unit

- **Status**: Proposed (awaiting owner review)
- **Date**: 2026-07-20
- **Arc**: picks up the piece [0064 §Deferred](0064-plugin-model.md) named and did not decide — *"agents/hooks (incl. event composition) plugin containment"* — and is the **receiver for [#2839](https://github.com/tya5/reyn/issues/2839)** (abolish the internal task system in favour of MCP-external orchestration).
- **Deferred (named, not decided here)**: multi-file skill bodies ([#3162](https://github.com/tya5/reyn/issues/3162)); time-triggered activation (the `cron` surface already exists and is not extended here); cross-session event observation (0059 §keeps this out of v1).

> Design contract: every "reyn already has X" claim in §2 is verified against `origin/main` at the cited path. Claims about *current behaviour* are deliberately anchored to locations rather than restated, because a restated mechanism goes stale and then misleads — this proposal exists partly because a stale `Status:` header in ADR-0039 caused its own author to design around a subsystem that was already built.

## 1. Context — what we are actually building

#2839 replaces reyn's internal task system with **MCP-external orchestration**. That decision moves the orchestrator *out* of reyn, which makes a question load-bearing that was previously cosmetic: **what does reyn core owe an external orchestrator so that it can be a plugin?**

The concrete driver used to find the gaps was a dynamic-UI plugin (a Streamlit app the agent edits, which reports breakage back so the agent can fix it). That is the **first consumer, not the motivation** — every gap below is a property of "something outside pushes, reyn reacts", not of Streamlit.

The loop a reactive plugin needs:

```
external system changes
  → server pushes a notification
  → reyn turns it into a hook event
  → an action runs (context staged / a turn woken / a pipeline launched)
  → a result travels back out
```

### 1.1 Why autonomous reaction is in-bounds

Everything here causes agent turns to run **when no human is present** — that is the point of a reactive plugin, and it needs saying explicitly because it is easy to mis-read as a constitutional problem.

It is not. "Agency is bounded **by construction**" is bounded by *typed, permissioned, auditable, rewindable ops* — the tagline names **spawn** and **orchestrate** as things agents legitimately do. Bounded does **not** mean "a human is watching". Reading it that way would forbid the self-driving behaviour reyn exists to provide.

The operative test is therefore four properties, not human presence:

| Property | Mechanism |
|---|---|
| **Permissioned** | the activation is granted by the operator (install + grant), not self-assumed |
| **Bounded** | a stable unit, not unbounded growth; runaway wakes hit `safety.loop.max_hook_driven_turns` |
| **Auditable** | a hook-driven push leaves a P6 trace (`hook_push_fired`) and any woken turn leaves the ordinary turn trail. *Measured caveat*: a bare cron job's fire itself emits **no dedicated P6 event** (`src/reyn/runtime/cron/` has no emit) — the trail starts at the hook push / the woken turn |
| **Killable** | stopping the reyn process stops all of it |

**`cron` is the standing precedent that this shape is already accepted**: an operator approves a job per-job at registration, the scheduler then resolves-or-spawns that job's own persistent session and boots its run-loop **with nobody watching**, a hook-driven push is audit-evented and the woken turn leaves the ordinary trail, and the whole thing exists only while the reyn process does. This proposal asks for the same shape for push-driven sources — it does not introduce a new class of autonomy.

## 2. Grounding — what reyn already has (verified on `origin/main`)

The recurring failure when designing here is **proposing a mechanism that exists**. This section is the anti-reinvention list.

| Need | Already provided | Location |
|---|---|---|
| Server→client push | `notifications/resources/updated` becomes the `mcp_resource_updated` hook event | `src/reyn/mcp/message_handler.py`, subscribe in `src/reyn/mcp/client.py` |
| Many distinct signals | **The URI is the namespace** — `matcher` glob-matches `uri` | `src/reyn/hooks/matcher.py` |
| Burst / flap suppression | Composer `window` / `debounce`, consumed as `composed:<name>` | `src/reyn/hooks/composer.py` |
| Interrupt vs ride-along | wake (inbox push) / no-wake (next-turn staging) / shell / `pipeline_launch` | `src/reyn/hooks/dispatcher.py` |
| Runaway bounding | `safety.loop.max_hook_driven_turns` | `src/reyn/config/chat.py` |
| Hot hook changes | live registry swap, reapplied at the turn boundary | `HookDispatcher.replace_registry` |
| Returning a result outward | `pipeline_launch` renders `input_template` against the event's template vars, runs async, result returns on this session's inbox (`src/reyn/hooks/dispatcher.py`) — and the launched pipeline's `shell` step is the write-back leg (`src/reyn/core/pipeline/parser.py`, which defines the `transform`/`tool`/`shell` step set) | see inline |
| Asking the human | MCP elicitation, per connection, with timeout + listener check | `src/reyn/mcp/connection_service.py` |
| Tearing a subscription down | `unsubscribe_mcp_resource` op | `src/reyn/tools/mcp.py` |
| Scoping | workspace `hooks:` + per-agent `.reyn/agents/<name>/hooks.yaml`; bus/registry are per-session | `load_per_agent_hooks`, `src/reyn/hooks/bus.py` |

**Consequences of this table** (things this proposal therefore does *not* add): no new event kind per signal; no server-side debounce; no callback/correlation convention; no crash-recovery story for external state (out of scope **by standing ruling** — the external world is not a reyn recovery source).

## 3. The gaps (measured)

**G1 — Activation is incoherent across three parts.** A reactive loop needs a held connection, a live subscription, and an installed hook. Today each has a *different* decider and timeline: the connection is **lazy** (opens when something calls a tool — `src/reyn/mcp/connection_service.py`), the subscription is **imperative** (an op the agent must call), and the hooks come from **config** (hot-reloaded at the turn boundary). There is no single "this orchestration is now running", and therefore no way to be *partially* correct — connected but unsubscribed, or hooked with nothing pushing.

**G2 — The LLM cannot register the hook this design needs.** `hooks_add` accepts only six lifecycle points (`turn_start`, `turn_end`, `session_start`, `session_end`, `task_start`, `task_end`) and only builds a `template_push` action (`src/reyn/tools/hooks.py`). `mcp_resource_updated` is **not** in that set and `pipeline_launch` is **not** constructible. So the "the agent decides to start an orchestration" pattern is structurally impossible today, even though the agent *can* already open the connection and subscribe.

**G3 — A plugin cannot ship its reactive wiring.** The capability union is `mcp` / `pipelines` / `skills` (`src/reyn/plugins/manifest.py`) and install registers into exactly those three registries (`src/reyn/core/op_runtime/plugin_install.py`). A plugin whose entire value is "react to my events" cannot deliver that value by being installed.

**G4 — Durability is asymmetric.** Hooks persist (a config file); the connection and subscription are volatile. After a crash the residue is *hooks that fire on nothing*.

**G5 — A no-wake staging can starve.** Next-turn ride-along is the correct default when a human is in the loop, but if no next turn ever comes it is never consumed. It must not be the only path for something that must be acted on.

## 4. Decision

### 4.1 One activation unit, three entry points

An **automation** is the triple *{hold this server, subscribe these URIs, install these hooks}* — activated together, torn down together, **scoped to a session**. This single unit closes G1, G3 and the scoping question at once: hooks are live exactly while the plugin is active in that session, so no new session-id bookkeeping is invented (the connection lifetime already *is* per-session).

**Scope of the unit — only for sources that require a held connection.** Of the four external event points, only `mcp_resource_updated` originates from a connection the session must hold and subscribe on. `file_changed` is an in-process watcher; `cron_fired` and `webhook_received` arrive **out-of-process** and resolve their target session themselves (`src/reyn/hooks/ingress.py`). Those three need **no activation unit at all — a hook alone is sufficient**, and for `cron` the operator pattern is already fully available today via the `reyn cron` CLI. This proposal's §4.1 therefore applies to MCP-push orchestration; §4.3 and §4.5 apply to all four.

Three ways to trigger it, by layer:

| Entry point | Surface | Role |
|---|---|---|
| **CLI** | `reyn <cmd>` (precedent: `plugin`, `mcp`, `cron`) | Workspace/agent layer: *may* be used here; always-on declaration |
| **slash** | `/<cmd>` (precedent: `/agent`, `/budget`) | This session, now — matches the session-scoped lifetime |
| **LLM op** | a typed op | On demand, **within the operator's grant** |

### 4.2 Authority split — content vs timing

**The operator authors what runs; the LLM decides when it is live.** The hook bodies, pipelines and subscribed URIs come from the plugin definition, reviewed when the operator installs it. The LLM only activates and deactivates.

This is what makes LLM-triggered activation safe without loosening `hooks_add`'s existing restrictions: the agent is not authoring a hook, it is switching on an operator-approved one.

### 4.3 `hooks_add` extension (G2)

Extend by the same discriminator — **who authored the content that runs**:

| Change | Decision | Rationale |
|---|---|---|
| Add **all four** external event points (`mcp_resource_updated`, `file_changed`, `cron_fired`, `webhook_received`) to the allowed `on:` set | **Add** | The single reason the LLM pattern cannot exist today. Runaways are bounded by the existing loop valve; no-wake pushes are benign. **`cron_fired` needs no carve-out**: registering a hook does not create a schedule — the schedule is created by `cron__register`, which is already gated by the `cron_register` permission key **and** a per-job operator approval prompt. Reacting to an approved job grants no new authority. |
| Allow the `pipeline_launch` action | **Add** | It launches a **registered** pipeline — content is operator-installed; the LLM chooses only which and when. Also the only path for returning a result outward. |
| Allow `exec` / `exec_capture` (renamed from `shell_exec`/`shell_push` in #3226 Phase 4) | **Keep restricted** | The LLM would *author the argv* — that hands over content authority, a different class from the two above. |

**Storage — an LLM-registered external-event hook is session-scoped and ephemeral.** `hooks_add` today persists to the workspace runtime layer (`.reyn/config/hooks.yaml`), hot-reloaded into **every** session. That is correct for the six lifecycle points it currently accepts, but for external event points it would contradict the scoping rule below (the reaction must be visible only to the registering session) and would let a hook **outlive the session whose activations its guard references** — a dangling scope. Ruling: `hooks_add` registrations on external event points go into the registering session's own per-session registry (the bus/registry are already per-session — `src/reyn/hooks/bus.py`) and **die with the session; `hooks.yaml` is never written**. Lifecycle-point registrations keep today's persistent semantics unchanged.

**URI scope for an LLM-registered external-event hook**: it may target only the event surface of a plugin **this session activated**, and the reaction is visible only in that session. A session must not be able to attach itself to another plugin's or another session's signals. (Same shape as 0059's tiering of the `on:` vocabulary.)

### 4.4 Stopping — converge to the crash residue

Stop tears down all three parts. Three rules:

1. **No partial stop.** A left-over subscription costs pushes nobody handles; a left-over hook accumulates as a dead entry.
2. **Stop leaves the same state a crash leaves.** This is the rule that fixes G4: rather than making the connection/subscription durable, the activation is defined as **volatile** — after a restart a session begins **inactive**. The operator pattern re-declares itself from config; the LLM pattern re-decides. Stop-path and failure-path then produce one state, and one test covers both.
3. **Idempotent.** Stopping twice, or stopping something never started, is a no-op, not an error.

In-flight work at stop time (a Composer buffer, a running pipeline, an armed escalation) is **discarded** — consistent with the Composer's existing best-effort posture rather than inventing a second durability story.

### 4.5 Inbox `escalate_after` (G5) — independent

An inbox entry queued **without** a wake may carry an `escalate_after`; on expiry it is promoted to a waking delivery through the **existing** wake path, so the existing loop valve counts and bounds it. Opt-in per producer (never a global default — "nobody asked, so it does not matter" is the correct semantics for most no-wake pushes). Escalation emits an audit event: a silent escalation is worse than none. Best-effort across a crash, matching §4.4.

This is a general inbox property, not a hook feature — every no-wake producer benefits. It is separable from §4.1–4.4 and can land independently.

## 5. Interfaces

Three surfaces, one activation unit behind them. Each follows the existing idiom of its surface rather than inventing a shape: CLI = argparse sub-parser + `set_defaults(func=...)` (`src/reyn/interfaces/cli/commands/cron.py`); slash = the `@slash(name, summary=...)` decorator (`src/reyn/interfaces/slash/`); LLM = a typed IR op (`kind: Literal[...]` on a pydantic model, `src/reyn/schemas/models.py`) surfaced as a catalog verb.

**Naming is deliberately shared across all three** — the noun **`automations`** and the two verbs `activate` / `deactivate` — so an operator reading an audit trail sees one concept regardless of which surface triggered it. The noun was chosen against two rejected candidates on measured grounds: **`triggers`** collides with reyn's existing internal vocabulary (`hook_trigger`, 27+ occurrences in `src/reyn/hooks/`), and **`reactions`** collides externally with the emoji-reaction sense the word now carries in Slack/GitHub/Discord. `automations` is unclaimed as an identifier (its only occurrences repo-wide are prose), reads unambiguously in both English and Japanese (自動化), and matches the real-world analogue closest to this unit — Home Assistant's `automations`, which likewise bundles trigger + condition + action.

### 5.1 Plugin interface — what a plugin declares

A fourth capability variant, mirroring the existing three (discriminated union, `entries` empty = discover by layout convention):

```python
class PluginAutomationsCapability(BaseModel):
    kind: Literal["automations"] = "automations"
    entries: tuple[str, ...] = ()      # empty = discover automations/*.yaml
```

Each declared file is one **activation definition**:

```yaml
# <plugin_root>/automations/<name>.yaml
name: streamlit_ui            # activation id, unique within the plugin
server: streamlit             # the .mcp.json server this activation holds
exclusive: true               # server owns an exclusive resource (a port, one UI)
subscribe:                    # URIs subscribed on activate, dropped on deactivate
  - "app://status"
composers:                    # optional — same schema as the workspace `composers:`
  - name: app_settled
    op: debounce
    ttl: 3
    on: mcp_resource_updated
    matcher: {server: streamlit, uri: "app://status"}
hooks:                        # same schema as the workspace `hooks:`
  - on: composed:app_settled
    template_push: {message: "...", wake: false}
```

Rules:

- **A plugin may only name its own `server:`** and may only subscribe/match URIs on that server. This is the containment 0064 deferred; it is what makes plugin-shipped hooks safe to install without a per-hook review. Enforced **at plugin load** (a definition naming a foreign server is rejected — enforce-at-load, 0059's posture) **and re-checked at activate**.
- **`exclusive: true` fails closed on a second session.** stdio transport spawns **one server subprocess per holding session**, so a server owning an exclusive resource (a bound port, a single UI — the Streamlit first consumer is exactly this) cannot be held twice: the second `activate` fails with an error **naming the holding session**, instead of a silent port clash or a half-broken duplicate. Non-exclusive servers (stateless, e.g. the RAG pair) multi-activate freely.
- **Activation is not access control.** The plugin's server is registered into `mcp.yaml` at install, and its *tools* remain callable from any session under the ordinary MCP permission gates whether or not an activation is live. The containment above bounds **reactions** (what may subscribe and fire), not tool access.
- **`exec` / `exec_capture` (renamed from `shell_exec`/`shell_push` in #3226 Phase 4) in a plugin-shipped hook requires an explicit grant at install time**, surfaced as its own approval line — same reasoning as §4.3: the plugin, not the operator, authored the argv.
- Registration is plugin-attributed so `plugin_uninstall` removes the definitions exactly like the existing three registries.
- **Declaring an activation does not activate it.** Shipping ≠ running; activation is always one of §5.2 / §5.3.

### 5.2 User interface — CLI (durable) and slash (this session)

**CLI — workspace/agent layer, durable.** New sub-parser group under the existing plugin command family:

| Command | Effect |
|---|---|
| `reyn automation list` | Every declared activation, its plugin, and whether it is granted / auto-start |
| `reyn automation grant <plugin>/<name> [--agent <a>]` | Permit LLM activation. **Without a grant, §5.3 fails closed.** |
| `reyn automation revoke <plugin>/<name>` | Withdraw the grant; deactivates it wherever it is live |
| `reyn automation autostart <plugin>/<name> --on\|--off [--agent <a>]` | Start automatically in every session of that agent |

**slash — this session, now.** `/automation` follows the `@slash` idiom:

| Command | Effect |
|---|---|
| `/automation` | What is active in THIS session, and what is available to activate |
| `/automation on <plugin>/<name>` | Activate here |
| `/automation off <plugin>/<name>` | Deactivate here |

`/automation off` works on an activation the LLM started — the operator is the higher authority (§4.4 rule 1 applies to whoever stops it).

**`autostart` semantics** (the deactivation lattice, stated so it need not be discovered later): `autostart` is an operator-surface action and therefore **requires no grant** — the grant of §5.2/§5.3 gates only the LLM op. `autostart` applies **at session start only**: a mid-session deactivate (either `/automation off` or the LLM op) sticks for the remainder of that session, and autostart re-applies at the next session start. No mid-session tug-of-war, no livelock.

### 5.3 LLM interface

Two ops, permission-gated on a single new axis (`require_automation_activate`), scoped per activation id:

```python
class AutomationActivateIROp(BaseModel):
    kind: Literal["automation_activate"]
    activation: str      # "<plugin>/<name>"

class AutomationDeactivateIROp(BaseModel):
    kind: Literal["automation_deactivate"]
    activation: str
```

Semantics:

- **Fails closed without an operator grant** (§5.2). The LLM cannot self-grant; this is the "operator authors content and availability, LLM decides timing" split of §4.2 made mechanical.
- **The grant lives in the permission layer, not a new registry** — the same shape as `require_cron_register` (a permission axis + per-item operator approval), persisted with the agent's permissions. No new config file is invented for grants.
- The LLM may deactivate **any** activation live in its session, including an autostart-ed one; per §5.2's `autostart` semantics the deactivation sticks for that session only.
- **Activate is all-or-nothing** — hold + subscribe + install, or none of it (§4.4 rule 1 in the forward direction).
- **Idempotent** both ways (§4.4 rule 3).
- Both emit an audit event carrying the activation id and the deciding surface, so a trail shows *who* started it, not merely that hooks fired.
- Deactivate is implicit at session end — activation is session-scoped and volatile (§4.4 rule 2).

**`hooks_add` is not extended for plugin activations.** The extension in §4.3 remains for hooks the LLM registers *directly*; a plugin's hooks arrive via activation, already authored. The two paths stay distinct so the `exec`/`exec_capture` boundary (renamed from `shell_exec`/`shell_push` in #3226 Phase 4) is enforced in one place per path.

### 5.4 What each surface may NOT do

| Surface | Cannot |
|---|---|
| Plugin | Name another plugin's server; subscribe outside its own server; ship an `exec` hook without an install-time grant |
| LLM | Grant itself; activate an ungranted activation; register a hook on another plugin's event surface; author an `exec` hook |
| Operator | *(no restriction — the operator is the authority both other surfaces derive from)* |

## 6. Implementation notes — build by isomorphism, not invention

- **Activation lifecycle ≅ plugin install/uninstall.** Attributed registration + idempotent teardown + converge-to-a-known-state is the shape `plugin_install` already implements (step-tracked progression; attributed removal in `plugin_uninstall`). Reuse that shape — do not grow a parallel lifecycle machine.
- **One hooks/composers schema, three carriers.** The workspace `hooks:` config, the per-agent layer, and the activation definition carry the **same** schema and must flow through the **same** loader/validation path (`load_hooks`). A second parser is how carriers drift apart.
- **Grant ≅ cron's per-job approval** (§5.3): one existing permission-axis pattern, reused — not a new grant store.

## 7. Verification obligations (for the implementing PRs)

Per repo discipline (ADR-0039 D8 carried a reachability + fail-close assert per phase; `docs/deep-dives/contributing/verification-hazards.md`):

- **All-or-nothing witness** (§5.3): force a mid-activation failure (e.g. the subscribe step fails) → assert **nothing** is left installed — no hook in the registry, no held connection.
- **Grant fail-close negative witness**: witness the **deny side** — an ungranted `*_activate` op is refused. A granted happy-path test alone cannot prove the gate exists.
- **Exclusivity witness** (§5.1): with an `exclusive: true` activation held by session A, session B's activate fails closed with the error naming A.
- **Session-scope witness for ephemeral hooks** (§4.3): register an external-event hook via `hooks_add`, end the session → the hook is gone **and `hooks.yaml` is byte-untouched**.
- **Escalation-through-valve witness** (§4.5): an `escalate_after` promotion flows through the ordinary wake path such that `max_hook_driven_turns` counts it — remove the valve interaction and the bound test must flip RED.
- **Minimal-strip discipline**: every strip-falsify names the **single** property it breaks (a broad revert that breaks several mechanisms at once proves nothing about any of them).

## 8. Consequences

- An external orchestrator becomes installable-and-it-works, which is the precondition #2839 needs before the internal task system can go.
- Activation becomes an auditable event with a single decider, instead of an emergent property of "someone happened to call a tool".
- The volatile-activation ruling (§4.4) means **a reactive plugin is not automatically running after a restart**. That is a deliberate trade: predictability and one recovery story, at the cost of a re-activation step. The operator pattern hides this; the LLM pattern does not.
- `hooks_add` becomes more capable, and the `exec`/`exec_capture` line becomes the explicit boundary of LLM hook authorship rather than an accident of what was implemented first.

## 9. Rejected alternatives

- **Make connection + subscription durable so activation survives a crash.** Rejected: it creates a second durability story next to the WAL, and the external world is out of recovery scope by ruling. §4.4 converges downward instead.
- **A per-session hook config layer keyed by session id.** Rejected: session ids are runtime-generated and re-keyable, so the file layer has no author. The connection lifetime already provides per-session scoping for free.
- **A correlation/callback mechanism for returning results.** Rejected: `pipeline_launch` + `input_template` + a `shell` step already closes the loop, with the correlation id carried in the URI.
- **Let `hooks_add` register `exec`.** Rejected per §4.3.
- **Extend `reyn_cheat_sheet` with the authoring guidance.** Rejected: it is at ~98.7% of the default read cap (#3162). The guidance ships as the sibling skill `reactive_orchestration_plugins`.
