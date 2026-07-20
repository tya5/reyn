---
name: reyn_cheat_sheet
description: Reyn-specific usage cheat sheet -- which mechanism to reach for (skill/pipeline/mcp/hook/present), composition idioms, op essentials, and pointers to the full specs. Read this before authoring a new part or composing several.
references:
  - input-hooks-and-mcp.md
  - workflow-pipelines-and-skills.md
  - output-present-and-self-review.md
---

# Reyn cheat sheet

This is the gap-filler between "reyn has these parts" and "you use them
correctly". concept/reference docs describe each mechanism in isolation;
this skill is the composition know-how in the cracks between them (0060
Addendum D1). Read on demand when deciding which mechanism to use, or before
authoring a new skill/pipeline/hook/present-view.

This L2 router stays deliberately small: it is enough on its own to decide
*whether* you need to go deeper and *which* `references/` file answers your
question -- you should not need to open a reference just to find out if you
need it.

## Decision tree (which mechanism)

- Need **input** (new data, or a reactive trigger) -> `hook` | `mcp` |
  `retrieval` (`semantic_search`). Deeper detail (hook action schemes, a
  CI-verified worked hook example, MCP tool discovery):
  `references/input-hooks-and-mcp.md` -- read when wiring a reactive
  trigger or calling an external tool/resource.
- Need **workflow** (multi-step orchestration) -> `skill` | `pipeline` | an
  `mcp` tool call mid-flow. Deeper detail (pipeline step grammar, the
  flagship CI-verified worked pipeline, `SKILL.md` authoring shape):
  `references/workflow-pipelines-and-skills.md` -- read when writing
  pipeline DSL steps or authoring a new skill.
- Need **output** (show a result, or write externally) -> `present` |
  `render_template` | an `mcp` write. Deeper detail (`present`'s blueprint
  catalog and the input/output caveat, the self-review gate before
  promotion): `references/output-present-and-self-review.md` -- read when
  deciding whether/how to show a result via `present`, or self-reviewing an
  authored asset before it becomes a reused part.

Reuse before authoring: check the existing catalog (`list_actions`) for a
part that already covers the need. Author only when nothing fits, and
self-review anything you author or promote (an `agent` step + `schema`, see
`references/output-present-and-self-review.md`) before it becomes a reused
asset -- an ungated authored part is a liability, not a shortcut.
