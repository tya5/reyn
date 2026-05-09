---
type: phase
name: search
input: user_message
role: mcp_researcher
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
              repo_url:    {type: string}
              description: {type: string}
            required: [name, repo_url, description]
        source:
          type: string
          description: "registry | registry_stale | error"
        query:
          type: string
      required: [candidates, source, query]
---

The MCP registry has already been queried by the OS preprocessor. Use only the data in
`data.registry` — do NOT call any ops or fetch any URLs.

## Step 1 — Check preprocessor result

`data.registry.source` tells you what happened:
- `"registry"` — fresh results from registry.modelcontextprotocol.io.
- `"registry_stale"` — registry was unreachable; these are cached results from up to 24h ago.
- `"error"` — registry unreachable and no cache available.

`data.registry.query` is the keyword that was searched.
`data.registry.candidates` is the list of servers returned (may be empty).

## Step 2 — Filter by relevance

From `data.registry.candidates`, keep only the servers relevant to the user's request.
A server is relevant if its name or description plausibly matches the capability asked for.

If `data.registry.source` is `"error"`, set `candidates: []` and note the failure in the
result. Do not attempt to fetch anything yourself — that would be a P3 violation.

## Step 3 — Return mcp_candidate_list

Finish with the `mcp_candidate_list` artifact containing only the relevant servers.

For each kept candidate:
- `name`: use `candidate.name` verbatim (e.g. `"capital.hove/read-only-local-postgres-mcp-server"`)
- `repo_url`: use `candidate.repo_url` verbatim
- `description`: use `candidate.description` verbatim — do NOT invent or paraphrase

If no candidates are relevant (or the list is empty), set `candidates: []`.

Note: Anthropic's official reference servers (e.g. for GitHub, filesystem, memory) are not
registered in the registry. If the user asks for them specifically, mention in a follow-up
that they can be found at `https://github.com/orgs/modelcontextprotocol/repositories`.
