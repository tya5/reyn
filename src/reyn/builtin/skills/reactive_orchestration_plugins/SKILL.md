---
name: reactive_orchestration_plugins
description: How to build something that REACTS to an external system (an orchestrator, a watcher, a UI, any MCP server that pushes) -- which reyn mechanism already answers each requirement, and the anti-pattern list of things people re-invent. Read this BEFORE designing any external-event-driven plugin, server-push handling, wake/notification behaviour, or browser UI integration. Companion to reyn_cheat_sheet (which covers choosing between skill/pipeline/mcp/hook/present in general).
---

# Reactive / orchestration plugins

reyn<->MCP is **bidirectional**: you call the server's tools, and the server
pushes back at you. The recurring failure when designing here is **proposing
a mechanism reyn already has**.

**How to read this file.** It names *where to look*, not what the code
currently does. A doc that restates behaviour goes stale and then actively
misleads -- that is exactly how this file's own author got six design
decisions wrong in one session, including reading a stale `Status:` header
and concluding an implemented subsystem did not exist. **Open the cited path
and confirm before you rely on it.** If a claim here and the code disagree,
the code wins and this file is the bug.

## Reuse map -- check here before designing anything

| You are about to build | Already exists |
|---|---|
| A new event kind per signal | **URI namespace** on one hook point |
| Burst/flap suppression in your server | **Composer** `window` / `debounce` |
| A callback convention to return results | **`pipeline_launch`** + a `shell` step |
| A channel to ask the human a question | **MCP elicitation** |
| A wire protocol for a browser UI | **AG-UI** |
| A crash-recovery story for external state | Nothing -- **out of scope by ruling** |

## Additional resources

These files live next to this one, under `references/`. `${CLAUDE_SKILL_DIR}`
expands to this file's own directory when SKILL.md is loaded -- read a link
below with the ordinary file read op when your question maps to it; do not
read all three unconditionally.

- [incoming-events-coalescing-and-wake.md](${CLAUDE_SKILL_DIR}/references/incoming-events-coalescing-and-wake.md)
  -- how a server-pushed notification reaches a hook
  (`mcp_resource_updated`), how to namespace distinct signals on the URI
  without new event kinds, how to coalesce a flapping/bursty source with
  the Composer, and how to choose wake=true (interrupt now) vs wake=false
  (ride-along). Read this when you are wiring the *inbound* side of a
  reactive plugin.
- [returning-results-and-elicitation.md](${CLAUDE_SKILL_DIR}/references/returning-results-and-elicitation.md)
  -- how to send a conclusion back out via `pipeline_launch` + a `shell`
  step (no callback convention needed), and how to ask the human a
  question via MCP elicitation (vs. sampling). Read this when your design
  needs a write-back leg or a human-in-the-loop question.
- [scope-plugin-surface-and-vocabulary.md](${CLAUDE_SKILL_DIR}/references/scope-plugin-surface-and-vocabulary.md)
  -- hook config scope (workspace vs per-agent, and why a workspace-level
  hook fires in every session), what a plugin manifest may actually ship,
  the AG-UI vs A2UI vocabulary split, three constraints that repeatedly
  surprise authors (progress is not a hook, a webhook body never reaches
  your matcher, nothing detects absence), and the skill-body size ceiling
  itself. Read this for plugin-packaging, config-scope, or UI-protocol
  questions.
