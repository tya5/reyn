# Workflow mechanisms: pipelines and skills

Read this when writing a `pipeline:` document's steps, or authoring a new
`SKILL.md` -- the two answers to the cheat sheet's decision tree "need
**workflow**" branch (multi-step orchestration).

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
invoke by name: `pipeline__run(name="flagship.research_and_report",
input={"query": "..."})`, not copy-pasted inline).

## Skills -- authoring a new one

A `SKILL.md` is YAML frontmatter (`name`, `description`) + a free-form
Markdown body -- not a schema the OS parses (the pre-1.0 phase-graph
`entry:`/`graph:`/`final_output:` shape is REMOVED). The registry never
reads the body -- only `path`/`description` populate the L1 menu; you read
the body yourself at L2 via the ordinary read op when its description looks
relevant. Install via `skill_management__install_local` /
`skill_management__install_source`. Full spec:
`docs/concepts/tools-integrations/skills.md`.
