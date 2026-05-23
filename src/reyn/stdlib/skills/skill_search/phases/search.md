---
type: phase
name: search
input: user_message
role: skill_researcher
can_finish: true
allowed_ops: []
preprocessor:
  - type: python
    module: ./registry_fetch.py
    function: fetch_registry_results
    into: data.registry
    output_schema:
      type: object
      properties:
        candidates:
          type: array
          items:
            type: object
            properties:
              name:        {type: string}
              source_url:  {type: string}
              description: {type: string}
            required: [name, source_url, description]
        source:
          type: string
          description: "registry | registry_stale | error"
        query:
          type: string
      required: [candidates, source, query]
---

The skills registry has already been queried by the OS preprocessor. Use only the data in
`data.registry` — do NOT call any ops or fetch any URLs.

## Step 1 — Check preprocessor result

`data.registry.source` tells you what happened:
- `"registry"` — fresh results from the registry.
- `"registry_stale"` — registry was unreachable; these are cached results from up to 24h ago.
- `"error"` — registry unreachable and no cache available.

`data.registry.query` is the keyword that was searched.
`data.registry.candidates` is the list of skills returned (may be empty).

## Step 2 — Filter by relevance

From `data.registry.candidates`, keep only the skills relevant to the user's request.
A skill is relevant if its name or description plausibly matches the capability asked for.

If `data.registry.source` is `"error"`, set `candidates: []` and note the failure in the
result. Do not attempt to fetch anything yourself — that would be a P3 violation.

## Step 3 — Return skill_candidate_list

Finish with the `skill_candidate_list` artifact containing only the relevant skills.

For each kept candidate:
- `name`: use `candidate.name` verbatim (e.g. `"pdf"`)
- `source_url`: use `candidate.source_url` verbatim (= the directly fetchable raw URL feeds into `skill_importer`)
- `description`: use `candidate.description` verbatim — do NOT invent or paraphrase

If no candidates are relevant (or the list is empty), set `candidates: []`.
