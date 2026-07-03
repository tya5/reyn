# write-readme

> 🔮 **Roadmap example.** Depends on: a `doc_writer` (or similarly named)
> stdlib component that produces prose/Markdown docs. Not runnable on Reyn v1
> as of 2026-05-02.
>
> Tracked in: post-OSS roadmap.

Use Reyn itself to draft a Reyn-style README for a target workflow. The
request goes to `reyn chat`, which classifies it as a generation task and
dispatches a doc-writing run to produce the artifact.

## What this shows

- `reyn chat` end-to-end: intent classification and delegated generation.
- That you don't need a custom workflow for "I want a README" — it's a
  directed generation request with the right framing.

## Run via chat (recommended)

```bash
reyn chat
> Write a Reyn-style README for the `mcp_search` stdlib component. Match the
  voice of the existing how-to guides. Output
  Markdown only.
```

The chat agent:

1. Routes the request to a doc-writing task.
2. Produces a draft README artifact.
3. Chat shows the rendered Markdown inline.

## Expected output

A `final_output` containing the README text. Save it:

```bash
reyn run my_doc_writer "..." > tmp/mcp_search-readme.md
```

(Reyn prints structured JSON; pull `.data.text` with `jq` if you want just
the prose.)

## Tip: matching house style

Mention a reference directory in the prompt — the model uses it as a
style anchor. `docs/concepts/` is reference-toned; the getting-started guides have a more tutorial voice.

## See also

- [Tutorial: chat mode](../../guide/getting-started/02-chat-mode.md)
