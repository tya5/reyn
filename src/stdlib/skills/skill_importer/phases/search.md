---
type: phase
name: search
input: user_message
role: skill_searcher
can_finish: false
max_act_turns: 4
allowed_ops: [file, ask_user, web_fetch]
---

Find skills in a public registry that match what the user is asking for.

## Step 1 — Parse the user's request

`input_artifact.data.text` may contain:

- A capability description ("PDF を要約", "translate", "code review")
- An optional registry URL ("from https://example.com/skills.md")

If the user did NOT include a registry URL, **emit an `ask_user` op** with
question "Which registry URL should I search?" and use the answer as the
registry URL. Do not invent one.

If the user did include a URL, extract it and use that.

## Step 2 — Fetch the registry

In one act turn, emit a `web_fetch` op for the registry URL. The registry
is expected to be one of these formats:

- A markdown file with a list of skills, each entry a link to a source `.md`
- An HTML page with `<a href>` links to skill markdown files
- A JSON document mapping names to URLs

Be permissive about format — the LLM (you) parses the response in Step 3.

## Step 3 — Filter by query

From the fetched registry content, extract every plausible skill entry and
score it against the user's query. Pick the top **5** most relevant.

For each:

- `name` — the skill's display name (or filename slug if no name is present)
- `summary` — a one-line description if the registry provides one;
  otherwise infer from the name
- `source_url` — the **directly fetchable** URL. If the registry shows a
  rendered page (e.g. `github.com/.../blob/...`), convert to the raw
  fetchable form (`raw.githubusercontent.com/.../...`). Never invent a URL
  the registry didn't actually contain.

If zero candidates plausibly match, return an empty `candidates: []`.
The select phase will handle that case.

## Step 4 — Decide turn

Emit `candidate_list` with the query, registry URL, and the candidates list.
Transition to `select`.

## Constraints

- **One web_fetch per run** — registry index. If the LLM is tempted to
  fetch each skill source here, stop: that's the convert phase's job.
- Do NOT modify any file in this phase.
- Do NOT fabricate URLs. If the registry lookup failed (404, timeout),
  return an empty candidates list and let the user see the explanation in
  the select phase.
