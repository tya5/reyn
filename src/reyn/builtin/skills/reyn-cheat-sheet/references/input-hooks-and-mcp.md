# Input mechanisms: hooks and MCP

Read this when wiring a reactive trigger (a hook) or reaching for an
external tool/resource/prompt provider (MCP) -- the two answers to the
cheat sheet's decision tree "need **input**" branch.

## Hooks -- reactive input, made visible

A hook fires an action at a lifecycle point (`session_start` / `turn_end` /
...) or an external-event point (`file_changed` / `mcp_resource_updated` /
`cron_fired` / `webhook_received`). Four action schemes: `template_push`
(inject context or self-continue), `exec` (sandboxed side effect, argv-list
only), `exec_capture` (stdout decides the push, argv-list only), `pipeline_launch` (launch a
registered pipeline, async). Hooks are operator-config only (`hooks:` in
`reyn.yaml`) -- you cannot author one yourself; `emit_hook_event` is the one
op that lets you put an event onto your OWN session's bus for an
operator-configured Composer/hook to react to. Full spec:
`docs/concepts/runtime/hooks.md`.

**Worked example -- a `file_changed` hook launching a pipeline** (this exact
text is CI-verified to load without a `HookConfigError`):

```yaml-hooks
hooks:
  - on: file_changed
    matcher: {path: "docs/**"}
    pipeline_launch:
      name: flagship.research_and_report
      input_template: {query: "summarize the change at {{ path }}"}
```

## MCP -- external capability

An MCP server is an external tool/resource/prompt provider, registered via
`mcp.servers` config. `describe_mcp_tool` gives a live round-trip spec for
any tool a connected server exposes. Full spec:
`docs/concepts/tools-integrations/mcp.md`.
