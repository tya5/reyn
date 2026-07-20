# Output mechanisms: present and self-review

Read this when deciding whether/how to show a result to the operator via
`present`, or when self-reviewing an authored/promoted asset before it
becomes a reused part -- the two answers to the cheat sheet's decision tree
"need **output**" branch.

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
