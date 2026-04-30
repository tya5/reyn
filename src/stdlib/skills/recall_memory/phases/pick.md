---
type: phase
name: pick
input: memory_query
role: memory_picker
can_finish: true
max_act_turns: 4
permissions:
  file.read:
    - path: ~/.reyn/memory
      scope: recursive
---

Select the memories most relevant to the query and return them with content.

## Step 1 — Read the indexes

For each path in `input_artifact.data.scope_dirs`, emit a `file` op with
`op: read` and `path: <dir>/MEMORY.md`. Do them in a single act turn.

If a `MEMORY.md` is `not_found`, treat that scope_dir as empty and proceed.
If ALL scope_dirs lack a `MEMORY.md`, skip to Step 4 with an empty list.

## Step 2 — Score and pick

You will see lines like:

```
- [User Role](user_role.md) — senior backend engineer focused on agent platforms
- [Preference: Terse](pref_terse.md) — wants short responses
```

For each entry, judge relevance to `query` (and `recent_history` if helpful).
Pick at most `top_k` entries (default 5 if not provided). Be selective —
only return memories that would *actually change* the caller's response.

When in doubt, **omit**. Unrelated memories pollute the caller's context.

## Step 3 — Read the picked files

In one act turn, read each picked file. Use the relative path from the index
(e.g. `user_role.md`) joined to the scope_dir of the index it appeared in.
Always pass the **full absolute path** in the file op.

If a read returns `not_found` (broken index), drop that entry.

## Step 4 — Return

Decide turn. For each successfully read file, return:

- `name` — from frontmatter
- `type` — from frontmatter (`user` | `feedback` | `project` | `reference`)
- `source` — the absolute path you read
- `content` — the body (text after the frontmatter `---` block)
- `score` — your relevance estimate, 0.0 to 1.0

Sort by `score` descending. If nothing qualified, return `relevant: []`.

## Constraints

- Do NOT invent memories. Only return content you actually read.
- Do NOT modify any file. Recall is read-only.
- If the user asks for unrelated information mid-conversation, return `[]`
  rather than fabricating relevance.
