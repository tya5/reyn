---
name: reyn_cheat_sheet
description: Reyn-specific usage cheat sheet -- which mechanism to reach for (skill/pipeline/mcp/hook/present), composition idioms, op essentials, and pointers to the full specs. Read this before authoring a new part or composing several.
---

# Reyn cheat sheet

This is the gap-filler between "reyn has these parts" and "you use them
correctly". concept/reference docs describe each mechanism in isolation;
this skill is the composition know-how in the cracks between them (0060
Addendum D1). Read on demand when deciding which mechanism to use, or before
authoring a new skill/pipeline/hook/present-view.

## Decision tree (which mechanism)

- Need **input** (new data, or a reactive trigger) -> `hook` | `mcp` | `retrieval` (`semantic_search`).
- Need **workflow** (multi-step orchestration) -> `skill` | `pipeline` | an `mcp` tool call mid-flow.
- Need **output** (show a result, or write externally) -> `present` | `render_template` | an `mcp` write.

Reuse before authoring: check the existing catalog (`list_actions`) for a
part that already covers the need. Author only when nothing fits, and
self-review anything you author or promote (an `agent` step + `schema`, see
below) before it becomes a reused asset -- an ungated authored part is a
liability, not a shortcut.

## `present` -- show results without spending tokens

`present(data_ref=..., blueprint=...)` (or `data_inline=...` for a value
already in hand) renders directly to the operator's UI at **zero token
cost to you** -- you never see the render, only a short ack. Use it for
RESULTS you want the operator to see (a status card, a table, a diff)
instead of dumping content into your own reply.

**The critical caveat, both directions matter:**

- **OUTPUT -> present.** Show results to the operator with `present` instead
  of pasting them into your reply.
- **INPUT -> read, never present.** Content YOU must read or act on (a
  skill's own body, a doc, a file you need to process) goes through the
  ordinary read op into your OWN context. Do **not** `present` it -- `present`
  renders to the operator's screen, not yours; presenting content you need to
  reason about means you never actually see it.

Blueprint catalog (8 components, display-only, non-executable): `text` /
`markdown` / `code` / `diff` / `keyvalue` / `table` / `list` / `image`. A
value inside a component binds via `{"$bind": "<json-pointer>"}` (RFC 6901)
against the presented data; anything else is a literal label. Full spec +
`$bind` grammar: `docs/reference/runtime/control-ir.md` (`present` section).

## Self-review -- gate before you promote (`agent` step + `schema`)

Score a value against your own checklist with a pipeline `agent` step whose
`schema:` names a small schema you declare (e.g. `{score: number, reason:
string}`) -- it **constrains generation and validates the parsed result**
(typed, validated scoring, not a bespoke op). Compare the parsed score
against your own threshold with a plain `transform` step. This is
**self-review, not objectivity**: you write the draft, you write the
checklist, and the same model family scores it -- useful, not independent.
Mandatory gate for auto-improvement promotion (0060 J-D). Full spec:
`docs/reference/runtime/pipeline-dsl.md`; worked loop: `draft_judge_revise`.

## Pipelines -- orchestration DSL essentials

A `pipeline:` document is a list of `steps:`; each step is single-key
(`transform` / `tool` / `shell` / `agent` / `call` / `match` / `fold` /
`for_each` / `parallel`). `output: NAME` on a step makes it readable as
`ctx.NAME` from every later step; the preceding step's own result is also
readable as bare `pipe`. A `tool`/`shell` argument is a literal unless
tagged `!expr EXPR` (an R1 expression against `ctx`/`pipe`); an `agent`
step's `prompt` interpolates `{ctx.dotted.path}` / `{pipe}` as a template
string. Full grammar: `docs/reference/runtime/pipeline-dsl.md`.

**A `tool` step's `ctx.<output>` is always the flat `{text, structured}`
shape** (uniform across every tool) -- never the tool's raw meta fields. An
`agent` step's `ctx.<output>` differs: no `schema:` -> plain-text reply;
`schema:` set -> the schema-validated PARSED value directly (e.g.
`ctx.verdict.score`, no `text`/`structured` wrapper).

**Worked example -- the flagship through-chain** (input -> workflow ->
output in one pipeline; this exact text is CI-verified to parse AND run):

```yaml
pipeline: research_and_report
description: >-
  Flagship through-chain exemplar (proposal 0060 F3) -- web_search -> agent
  (summarize) -> agent (self-review, schema-validated) -> present (zero-token
  operator output). Shows the input -> workflow -> output composition thesis
  end to end. Ships builtin + inert (invoke-by-name only, never auto-launched).
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
  - agent:
      prompt: >-
        Self-review this summary against your own checklist: is it accurate,
        is it concise, and does it directly answer the query "{ctx.query}"?
        Summary: {ctx.summary}. Give a score in [0.0, 1.0] and a short reason.
      schema: Verdict
      output: verdict
  - transform: {value: "ctx.verdict.score >= 0.6", output: passed}
  - tool:
      name: present
      args:
        data_inline: !expr "{summary: ctx.summary, verdict: ctx.verdict, passed: ctx.passed}"
        blueprint:
          - component: markdown
            text: {$bind: /summary}
          - component: keyvalue
            rows:
              - {label: score, value: {$bind: /verdict/score}}
              - {label: passed, value: {$bind: /passed}}
              - {label: reason, value: {$bind: /verdict/reason}}
      output: shown
---
schema: Verdict
fields:
  score: {type: number}
  reason: {type: string}
```

This ships as the builtin pipeline `flagship.research_and_report` (inert --
invoke by name: `run_pipeline(name="flagship.research_and_report",
input={"query": "..."})`, not copy-pasted inline).

## Hooks -- reactive input, made visible

A hook fires an action at a lifecycle point (`session_start` / `turn_end` /
...) or an external-event point (`file_changed` / `mcp_resource_updated` /
`cron_fired` / `webhook_received`). Four action schemes: `template_push`
(inject context or self-continue), `shell_exec` (sandboxed side effect),
`shell_push` (stdout decides the push), `pipeline_launch` (launch a
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

## Skills -- authoring a new one

A `SKILL.md` is YAML frontmatter (`name`, `description`) + a free-form
Markdown body -- not a schema the OS parses (the pre-1.0 phase-graph
`entry:`/`graph:`/`final_output:` shape is REMOVED). The registry never
reads the body -- only `path`/`description` populate the L1 menu; you read
the body yourself at L2 via the ordinary read op when its description looks
relevant. Install via `skill_management__install_local` /
`skill_management__install_source`. Full spec:
`docs/concepts/tools-integrations/skills.md`.

## MCP -- external capability

An MCP server is an external tool/resource/prompt provider, registered via
`mcp.servers` config. `describe_mcp_tool` gives a live round-trip spec for
any tool a connected server exposes. Full spec:
`docs/concepts/tools-integrations/mcp.md`.
