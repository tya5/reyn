---
name: reyn_cheat_sheet
description: Reyn-specific usage cheat sheet -- which mechanism to reach for (skill/pipeline/mcp/hook/present), composition idioms, op essentials, and pointers to the full specs. Read this before authoring a new part or composing several.
---

# Reyn cheat sheet

This is the gap-filler between "reyn has these parts" and "you use them
correctly". concept/reference docs fully describe each mechanism in
isolation; this skill is the composition know-how and the op essentials that
fall in the cracks between them (proposal 0060 Addendum D1). Read this on
demand when deciding which mechanism to use, or before authoring a new
skill/pipeline/hook/present-view.

## Decision tree (which mechanism)

- Need **input** (new data, or a reactive trigger) -> `hook` | `mcp` | `retrieval` (`semantic_search`).
- Need **workflow** (multi-step orchestration) -> `skill` | `pipeline` | an `mcp` tool call mid-flow.
- Need **output** (show a result, or write externally) -> `present` | `render_template` | an `mcp` write.

Reuse before authoring: check the existing catalog (`list_actions`) for a
part that already covers the need. Author only when nothing fits, and gate
anything you author or promote with `judge_output` before it becomes a
reused asset (an ungated authored part is a liability, not a shortcut).

## `present` -- show results without spending tokens

`present(data_ref=..., blueprint=...)` (or `data_inline=...` for a value
already in hand) renders directly to the operator's UI at **zero token
cost to you** -- you never see the render, only a short ack. Use it for
RESULTS you want the operator to see (a status card, a table, a diff) instead
of dumping the content into your own reply.

**The critical caveat, both directions matter:**

- **OUTPUT -> present.** Show results to the operator with `present` instead
  of pasting them into your reply.
- **INPUT -> read, never present.** Content YOU must read or act on (a
  skill's own body, a doc, a file you need to process) goes through the
  ordinary read op into your OWN context. Do **not** `present` it -- `present`
  renders to the operator's screen, not yours; presenting content you need to
  reason about means you never actually see it.

Blueprint catalog (8 components, all display-only, non-executable by
construction): `text` / `markdown` / `code` / `diff` / `keyvalue` / `table` /
`list` / `image`. A value inside a component binds via
`{"$bind": "<json-pointer>"}` (RFC 6901) against the presented data; anything
else is a literal label. Full spec + `$bind` grammar:
`docs/reference/runtime/control-ir.md` (`present` section).

## `judge_output` -- gate before you promote

`judge_output(data_inline=<value>, rubric="...", threshold=0.8)` scores a
value 0.0-1.0 against your own rubric and returns `{score, passed, reason}`.
Use `data_inline` for a value you already have (a prior pipeline step's
output, a draft you just produced) -- `target` is a legacy dot-path form for
the old phase-graph runtime only; supply exactly one. This is the mandatory
gate for auto-improvement promotion (0060 J-D): an authored/promoted part
earns catalog registration by passing a rubric, not by your own say-so. Full
spec: `docs/reference/runtime/control-ir.md` (`judge_output` section).

## Pipelines -- orchestration DSL essentials

A `pipeline:` document is a list of `steps:`; each step is single-key
(`transform` / `tool` / `shell` / `agent` / `call` / `match` / `fold` /
`for_each` / `parallel`). `output: NAME` on a step makes it readable as
`ctx.NAME` from every later step; the immediately preceding step's own result
is also readable as bare `pipe`. A `tool`/`shell` argument is a literal
unless tagged `!expr EXPR` (an R1 expression against `ctx`/`pipe`); an
`agent` step's `prompt` instead interpolates `{ctx.dotted.path}` / `{pipe}`
as a template string. Full grammar + the R1 expression language:
`docs/reference/runtime/pipeline-dsl.md`.

**A `tool` step's `ctx.<output>` is always the flat `{text, structured}`
shape** (uniform across every tool, mirroring what the chat side sees) --
never the tool's raw meta fields. `judge_output`'s `score`/`passed` reach a
downstream step via `ctx.<name>.structured.score` /
`ctx.<name>.structured.passed`, not `ctx.<name>.score` directly.

**Worked example -- the flagship through-chain** (input -> workflow ->
output in one pipeline; this exact text is CI-verified to parse AND run):

```yaml
pipeline: research_and_report
description: >-
  Flagship through-chain exemplar (proposal 0060 F3) -- web_search -> agent
  (summarize) -> judge_output (self-review) -> present (zero-token operator
  output). Shows the input -> workflow -> output composition thesis end to
  end. Ships builtin + inert (invoke-by-name only, never auto-launched).
steps:
  - tool:
      name: web_search
      args: {query: !expr ctx.query, max_results: 5}
      output: results
  - agent:
      prompt: >-
        Summarize these web search results into a concise, accurate answer
        to the query "{ctx.query}". Search results: {ctx.results}
      output: summary
  - tool:
      name: judge_output
      args:
        data_inline: !expr ctx.summary
        rubric: "Score 0.0-1.0: is the summary accurate, concise, and does it directly answer the query? Reply as JSON {\"score\": <0-1 float>, \"reason\": <string>}."
        threshold: 0.6
      output: verdict
  - tool:
      name: present
      args:
        data_inline: !expr "{summary: ctx.summary, verdict: ctx.verdict}"
        blueprint:
          - component: markdown
            text: {$bind: /summary}
          - component: keyvalue
            rows:
              - {label: score, value: {$bind: /verdict/structured/score}}
              - {label: passed, value: {$bind: /verdict/structured/passed}}
              - {label: reason, value: {$bind: /verdict/structured/reason}}
      output: shown
```

This same definition ships as the builtin pipeline `flagship.research_and_report`
(inert -- invoke it by name with `run_pipeline(name="flagship.research_and_report",
input={"query": "..."})` rather than copy-pasting it inline).

## Hooks -- reactive input, made visible

A hook fires an action at a lifecycle point (`session_start` / `turn_end` /
...) or an external-event point (`file_changed` / `mcp_resource_updated` /
`cron_fired` / `webhook_received`). Four action schemes:
`template_push` (inject context or self-continue), `shell_exec` (sandboxed
side effect), `shell_push` (a command whose stdout decides the push), and
`pipeline_launch` (launch a registered pipeline, async). Hooks are
operator-config only (`hooks:` in `reyn.yaml`) -- you cannot author a
`hooks:` entry yourself; `emit_hook_event` is the one op that lets you put an
event onto your OWN session's bus for an operator-configured Composer/hook to
react to. Full spec: `docs/concepts/runtime/hooks.md`.

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

## Skills -- authoring a new one

A `SKILL.md` is YAML frontmatter (`name`, `description`) + a free-form
Markdown body -- not a schema the OS parses (the pre-1.0 phase-graph
`entry:`/`graph:`/`final_output:` shape is REMOVED; do not copy an old
fixture using those keys). The registry never reads the body -- only
`path`/`description` populate the L1 menu; you read the body yourself at L2
via the ordinary read op when its description looks relevant. Install via
`skill_management__install_local` / `skill_management__install_source`. Full
spec: `docs/concepts/tools-integrations/skills.md`.

## MCP -- external capability

An MCP server is an external tool/resource/prompt provider, registered via
`mcp.servers` config. `describe_mcp_tool` gives you a live round-trip spec
for any tool a connected server exposes. Full spec:
`docs/concepts/tools-integrations/mcp.md`.
