# write-readme

> 🔮 **Roadmap example.** Depends on: a `doc_writer` (or similarly named)
> stdlib skill that produces prose/Markdown docs. Not runnable on Reyn v1
> as of 2026-05-02.
>
> Tracked in: post-OSS roadmap. Note: `skill_builder` (which this recipe
> originally called) is for **generating new skills** from a description —
> it writes `skill.md`, phase files, and artifact schemas under
> `reyn/local/<name>/`. It will not produce a free-form README for an
> existing skill. The meta-example shape below is the eventual intent;
> today it would route to `skill_builder` and produce a skill scaffold,
> not a README.

Use Reyn itself to draft a Reyn-style README for a target skill. Meta
example — the request goes to `reyn chat`, the chat agent's `skill_router`
classifies it as a generation task and dispatches a doc-writer skill to
produce the artifact.

## What this shows

- `reyn chat` end-to-end: implicit routing via `skill_router`, then a
  delegated `skill_builder` run.
- That you don't need a custom skill for "I want a README" — it's a
  skill_builder request with the right framing.

## Run via chat (recommended)

```bash
reyn chat
> Write a Reyn-style README for the `mcp_search` stdlib skill. Match the
  voice of the existing how-to guides under docs/guide/for-skill-authors/. Output
  Markdown only.
```

The chat agent:

1. Routes to `skill_builder` (intent: task, target: docs).
2. `skill_builder` produces a draft README artifact.
3. Chat shows the rendered Markdown inline.

## Run directly via skill_builder

If you'd rather skip routing and call the builder explicitly:

```bash
reyn run skill_builder "Write a Reyn-style README for the mcp_search stdlib skill, matching docs/guide/for-skill-authors/ voice. Output Markdown."
```

`skill_builder` is general-purpose and will recognize this as a docs task
rather than a "build a new skill" task.

## Expected output

A `final_output` containing the README text. Save it:

```bash
reyn run skill_builder "..." > tmp/mcp_search-readme.md
```

(Reyn prints structured JSON; pull `.data.text` with `jq` if you want just
the prose.)

## Tip: matching house style

Mention the reference directory in the prompt — the model uses it as a
style anchor. `docs/guide/for-skill-authors/` has consistent voice; `docs/concepts/`
is more reference-toned.

## See also

- [stdlib/skill_builder](../../src/reyn/stdlib/skills/skill_builder/skill.md)
- [stdlib/skill_router](../../src/reyn/stdlib/skills/skill_router/skill.md)
- [Tutorial: chat mode](../../docs/guide/getting-started/05-chat-mode.md)
