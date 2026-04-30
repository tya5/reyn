---
type: phase
name: extract
input: memory_extract_request
role: memory_writer
can_finish: true
max_act_turns: 5
allowed_ops: [file]
permissions:
  file.read:
    - path: ~/.reyn/memory
      scope: recursive
  file.write:
    - path: ~/.reyn/memory
      scope: recursive
---

Extract durable, memorable facts from the conversation segment and persist
them to the right scope. Writes both the per-memory file and updates
`MEMORY.md`.

## Step 1 — Read existing indexes (act turn 1, ONCE)

For each `scope_dir` in input, emit a `file` op `read` for `<path>/MEMORY.md`.
Do them all in **one act turn**. Treat `not_found` as "the index doesn't
exist yet — create it on first write".

**Never re-read MEMORY.md in subsequent act turns.** Once you have the
index contents (or confirmed they're absent — `not_found` counts as Step 1
done), move to Step 2.

If a `not_found` MEMORY.md returned, **do not retry**. The file genuinely
doesn't exist yet. You will create it in Step 3 if there is anything to
save, or skip directly to Step 4 (decide turn) with `op: none` if not.

## Step 2 — Analyze the conversation

Look at `conversation_segment`. Decide what (if anything) should be persisted.

### What to save

- `user` — durable facts about who the user is, their role, expertise areas,
  long-running preferences.
- `feedback` — explicit corrections / approvals about how to work
  ("don't summarize at the end", "always use poetry not pip").
- `project` — current initiatives, deadlines, decisions, who owns what.
  Convert relative dates to absolute (`Thursday` → use the conversation's
  most recent ts or today as anchor).
- `reference` — pointers to external systems (Linear projects, dashboards,
  Slack channels) and what to use them for.

### What NOT to save

- Code patterns / architecture / file paths — the codebase is authoritative.
- Git history, who-changed-what — git log/blame is authoritative.
- Debugging solutions or fix recipes — the fix is in the code; the commit
  has the context.
- Ephemeral task state, in-progress work, conversation-local context.
- Anything you'd write that's already implied by the project structure.

When in doubt, **don't save**. False memories pollute future recall more than
missing ones do.

### Updating vs creating

- If an existing memory's content needs to grow (new info on the same topic),
  emit `op: update`.
- If it's a wholly new topic, emit `op: create`.
- `delete` is rare — only when the user explicitly says "forget X" or the
  memory turned out wrong.
- If nothing memorable, **skip Step 3 entirely** and go straight to the
  decide turn (Step 4) with `actions: [{"op": "none", "rationale": "..."}]`.
  Do NOT emit any more act turns — empty file ops do not count as Step 3.

### Picking scope

- `global` — facts about the user-as-a-person that span projects (role,
  long-term preferences, working style).
- `project` — anything tied to the current codebase / project / deliverable.

When ambiguous, prefer `project`. Cross-project elevation is easy later;
overgrowth of global memory is harder to clean up.

## Step 3 — Write the files (act turn 2)

In **one act turn**, emit all the file ops needed to materialize your
decisions. Two kinds of ops:

### Per-memory body files

Each memory belongs to **exactly one** scope_dir — the one matching its
chosen `scope` (global → the scope_dir with `scope: "global"`, project →
the scope_dir with `scope: "project"`). **Never write the same memory to
both.**

For each `create`/`update`: one `file` op with `op: write`,
path `<chosen_scope_dir>/<slug>.md`, content:

```markdown
---
name: <Name>
description: <one-line>
type: user|feedback|project|reference
---

<body text>
```

For `update`, write the full new body (overwrite). For `delete`, use
`op: delete`.

#### Slug naming convention (REQUIRED)

The slug filename MUST follow `<type>_<topic>.md` where:

- `<type>` is one of `user`, `feedback`, `project`, `reference` —
  matching the memory's `type` field exactly.
- `<topic>` is 1–3 lowercase underscored words capturing the subject.

Examples:

- ✓ `user_role.md`, `user_python_expertise.md`
- ✓ `feedback_terse_replies.md`, `feedback_no_summaries.md`
- ✓ `project_memory_layer.md`, `project_q2_deadline.md`
- ✓ `reference_linear_ingest.md`, `reference_grafana_latency.md`
- ✗ `user.md` — too generic, will collide
- ✗ `response_style.md` — missing type prefix
- ✗ `current_project.md` — missing type prefix

Before writing a new memory, scan the existing index (from Step 1) for
slugs with the same prefix. If your topic is the same as an existing one
(possibly with a slightly different phrasing), use **`op: update`** with
the existing slug rather than creating a near-duplicate.

### The MEMORY.md index file

Every `scope_dir` has exactly **one** index file, literally named
`MEMORY.md`, that lists all memories in that scope. There is **NO** separate
`<slug>_index.md` per memory — only the bodies (`<slug>.md`) and the single
shared `MEMORY.md`.

For each `scope_dir` you wrote at least one body to this turn, emit
**exactly one** `file` op:

- `op: write`
- `path: <scope_dir>/MEMORY.md`   (verbatim — the filename is "MEMORY.md")
- `content`: the full reconstructed index. Each entry uses **just the slug
  filename** (e.g. `user_role.md`), not an absolute path — the body lives in
  the same directory as `MEMORY.md`.

```markdown
# Memory Index

- [Name 1](slug_1.md) — description 1
- [Name 2](slug_2.md) — description 2
```

Reconstructed index = existing entries (from Step 1's read) + your new ones
- any deleted ones. **Always use `write` (full overwrite), never `edit`** —
this works whether or not the index existed before.

## Step 4 — Decide turn (after writes complete)

Return `actions`: one entry per memory considered. For each:

- `op` — `create` | `update` | `delete` | `none`
- `scope` — `global` | `project`  (omit for `op: none`)
- `name` — the memory name (omit for `op: none`)
- `type` — category (omit for `op: none`)
- `rationale` — one-line why

If you saved nothing, return a single `actions: [{"op": "none", "rationale": "no durable facts in this segment"}]`.

## Constraints

- Use only paths from `input_artifact.data.scope_dirs[].path`. Do NOT invent
  destinations.
- Do NOT save secrets, credentials, or PII you wouldn't want in a markdown file.
- Body language matches the user's primary conversation language; keep it
  concise (under 5 lines per memory typically).
