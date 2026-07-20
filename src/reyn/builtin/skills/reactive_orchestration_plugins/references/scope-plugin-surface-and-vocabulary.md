## Scope -- a workspace-level hook fires in EVERY session

Config layers: the workspace `hooks:` config, and per-agent under
`.reyn/agents/<name>/hooks.yaml` (`load_per_agent_hooks` in
`src/reyn/config/loader.py`). The bus and registry are per-session
(`src/reyn/hooks/bus.py`).

If your reaction is only meaningful while a particular server is attached,
say so in the design -- otherwise unrelated sessions react to your events.

## What a plugin may ship

The capability union is `src/reyn/plugins/manifest.py`; what install actually
registers is `src/reyn/core/op_runtime/plugin_install.py`. **Read both before
assuming your plugin can ship a given part** -- the set is smaller than the
set of things reyn supports.

## Vocabulary -- two different UI protocols

Never write bare "UI protocol"; they are not interchangeable.

- **AG-UI** -- the agent<->UI **wire transport** (event stream + turn
  submit). This is what a browser or a thin client speaks to reyn:
  `src/reyn/interfaces/transport/agui/`.
- **A2UI** -- an agent-generated-UI **component spec**. reyn's present layer
  is *structurally isomorphic* to it, which is **not** the same as speaking
  it on the wire. See `docs/deep-dives/proposals/0054-present-layer.md`.

## Three constraints that surprise people

**Progress is not a hook.** `notifications/progress` is deliberately NOT
bridged to a hook point — the SDK already dual-delivers it to the per-call
`progress_callback` of the call *reyn itself made*. Your server's own
background-job progress has no such call to ride. Publish progress as a
**resource** instead (`orch://job/<id>/progress`) and let the ordinary
subscribe→`mcp_resource_updated` path carry it.

**A webhook's body never reaches your matcher.** `webhook_received` delivers
routing metadata ONLY (`transport`, `sender`) — the raw request body is never
put in template_vars, so tokens/PII cannot leak there
(`src/reyn/hooks/ingress.py`, the adapter's SECURITY invariant). You can match
"something arrived from X", not "the amount was 500". Content-driven flows
must fetch the content themselves at the gateway-plugin layer.

**Nothing detects absence.** Every Composer op (`all`/`any`/`seq`/`window`/
`debounce`/`correlate_by`/`count`) composes events that *happened*. There is
no "fire if Y did NOT arrive within T" — so deadman monitoring, heartbeat-gone
alerts, and approval-timeout escalation are not expressible today. Do not
mistake an inbox `escalate_after` for this: that watches an unconsumed inbox
entry, not a missing external event.

## Size ceiling for skills

A skill body is read through the ordinary read op, whose inline cap is
**derived from the model window** (`_read_inline_cap` in
`src/reyn/core/op_runtime/file.py`). With no model resolved the cap is small.
A body over the cap comes back `status: "truncated"` -- the reader silently
gets a partial skill. Keep a skill body well under it, and prefer a new
sibling skill over growing an existing one past its ceiling.
