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
    - path: .reyn/memory
      scope: recursive
  file.write:
    - path: .reyn/memory
      scope: recursive
---

Extract durable, memorable facts from the conversation segment and persist
them under `.reyn/memory/`. Writes both the per-memory body file and updates
`MEMORY.md`.

## Step 1 — Read the existing index (act turn 1, ONCE)

Emit a single `file` op `read` for `.reyn/memory/MEMORY.md`. Treat
`not_found` as "the index doesn't exist yet — create it on first write".

**Never re-read MEMORY.md in subsequent act turns.** Once you have the
index contents (or confirmed they're absent — `not_found` counts as Step 1
done), move to Step 2.

If `not_found` returned, **do not retry**. The file genuinely doesn't exist
yet. You will create it in Step 3 if there is anything to save, or skip
directly to Step 4 (decide turn) with `op: none` if not.

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

**Before deciding `create`, scan Step 1's index for any existing entry whose
topic overlaps the fact you're about to save**, even when phrased differently.
The check is semantic, not string-equal:

- `User Role` (existing: "backend engineer with 10y Python") vs new fact
  "I also use Rust" → **update** the existing entry, do not create
  `User Languages`.
- `Pref: Terse` (existing) vs new fact "I want short replies" → **update**
  (or skip — it may already cover this).
- `Python Experience` (existing: "10 years") vs new fact "I started Rust
  recently" → these are different topics; create a new `Rust Experience`
  entry, but consider whether the original memory's name should be
  generalized to `Programming Languages` via update.

Same-topic memories under different slugs are a bug, not a feature — they
fragment recall and let conflicting facts coexist. When in doubt, **update
the existing entry** rather than creating a near-duplicate.

Decision rules:

- If an existing memory's content needs to grow (new info on the same topic),
  emit `op: update` with that memory's existing slug.
- If it's a wholly new topic with no overlap in the index, emit `op: create`.
- `delete` is rare — only when the user explicitly says "forget X" or the
  memory turned out wrong.
- If nothing memorable, **skip Step 3 entirely** and go straight to the
  decide turn (Step 4) with `actions: [{"op": "none", "rationale": "..."}]`.
  Do NOT emit any more act turns — empty file ops do not count as Step 3.

## Step 3 — Write the files (act turn 2)

In **one act turn**, emit all the file ops needed to materialize your
decisions. Two kinds of ops:

### Per-memory body files

For each `create`/`update`: one `file` op with `op: write`,
path `.reyn/memory/<slug>.md`, content:

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

There is exactly **one** index file at `.reyn/memory/MEMORY.md` that lists
all memories. There is **NO** separate `<slug>_index.md` per memory — only
the bodies (`<slug>.md`) and the single shared `MEMORY.md`.

If you wrote at least one body this turn, emit **exactly one** `file` op:

- `op: write`
- `path: .reyn/memory/MEMORY.md`   (verbatim — the filename is "MEMORY.md")
- `content`: the full reconstructed index, in the **exact format** below.

#### Index entry format (REQUIRED — every line MUST match)

```
- [Name](slug.md) — description
```

Every entry line MUST contain all four parts:

1. `- ` (hyphen + space)
2. `[Name]` — the memory's name (matches the body's frontmatter `name`)
3. `(slug.md)` — bare slug filename, no path prefix
4. ` — description` — em-dash (` — `) plus the **same one-line description
   from the body's frontmatter `description` field**, copied verbatim

The description after the em-dash is **load-bearing**: callers (e.g. the
chat router) decide whether the index entry is enough to answer a question
or whether they need to open the body. Skipping the description forces every
caller to open every body — wasteful and slow.

Concrete examples:

```markdown
# Memory Index

- [User Role](user_role.md) — Backend engineer with 10 years of Python experience
- [Preference: Terse](feedback_terse.md) — Wants short replies, no trailing pleasantries
- [Project: Memory Layer](project_memory_layer.md) — Active rewrite to inject MEMORY.md as router preprocessor input
```

Wrong forms (do NOT produce these):

- ✗ `- [User Role](user_role.md)` — missing description
- ✗ `- [User Role](user_role.md) - description` — wrong dash (must be ` — `)
- ✗ `- User Role: backend engineer...` — missing markdown link
- ✗ `- [User Role](.reyn/memory/user_role.md) — ...` — path prefix forbidden

Reconstructed index = existing entries (from Step 1's read, parsed and kept
intact) + your new entries - any deleted ones. **Always use `write` (full
overwrite), never `edit`** — this works whether or not the index existed
before.

When carrying over existing entries from Step 1's read, copy the entire
line verbatim (including its description). Do NOT rewrite descriptions for
memories you are not creating/updating this turn.

## Step 4 — Decide turn (after writes complete)

Return `actions`: one entry per memory considered. For each:

- `op` — `create` | `update` | `delete` | `none`
- `name` — the memory name (omit for `op: none`)
- `type` — category (omit for `op: none`)
- `rationale` — one-line why

If you saved nothing, return a single `actions: [{"op": "none", "rationale": "no durable facts in this segment"}]`.

## Constraints

- All paths MUST start with `.reyn/memory/`. Do NOT write outside this
  directory.
- Do NOT save secrets, credentials, or PII you wouldn't want in a markdown file.
- Body language matches the user's primary conversation language; keep it
  concise (under 5 lines per memory typically).
