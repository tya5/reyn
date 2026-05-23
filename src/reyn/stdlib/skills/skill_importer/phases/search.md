---
type: phase
name: search
input: user_message
role: skill_searcher
can_finish: false
max_act_turns: 4
allowed_ops: [file, web_fetch]
---

Find skills in a public registry that match what the user is asking for.

> Most users should call ``skill_search`` first for richer discovery
> (= keyword + description match, 24h cache, scoped to a known
> registry). This phase is the fallback path used when the caller
> hands skill_importer a free-form user message *with no registry
> URL*. The chat router may prefer chaining
> ``skill_search → skill_importer`` for the best UX.

## Step 1 — Parse the user's request

`input_artifact.data.text` may contain:

- A capability description ("PDF を要約", "translate", "code review")
- An optional registry URL ("from https://example.com/skills.md")

**If the user included a URL**, extract it and use that as the registry URL.

**If the user did NOT include a URL**, default to the canonical Anthropic
skills registry as a directory listing:

```
https://api.github.com/repos/anthropics/skills/contents/skills
```

Override via the ``REYN_SKILL_REGISTRY_URL`` environment variable if set
on the host. Do not ask the user for the URL — the default is good enough
for the common case, and ``skill_search`` is the discovery surface when
the user wants something other than the default registry.

## Step 2 — Fetch the registry

In one act turn, emit a `web_fetch` op for the registry URL. The registry
is expected to be one of these formats:

- A markdown file with a list of skills, each entry a link to a source `.md`
- An HTML page with `<a href>` links to skill markdown files
- A JSON document mapping names to URLs (= what the GitHub Contents API
  returns for the default registry above; each entry has ``name`` +
  ``html_url``; the raw source markdown lives at
  ``raw.githubusercontent.com/<owner>/<repo>/main/skills/<name>/SKILL.md``)

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
