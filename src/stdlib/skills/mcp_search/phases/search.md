---
type: phase
name: search
input: user_message
role: mcp_researcher
can_finish: true
---

Search the GitHub MCP Registry for servers relevant to the user's capability request.

## Step 1 — Fetch the registry

Fetch the GitHub MCP Registry page to get the curated server list:

```
web_fetch: https://github.com/mcp
prompt: Extract every MCP server entry as a list of {path, name} pairs. Each entry appears as a
        link in the form /mcp/{owner}/{repo}. Ignore navigation links (blob, tree, graphs, issues,
        pulls, actions, wiki, security, pulse, community, announcement, beta, dist, bridges, main).
```

## Step 2 — Fetch each candidate's description

For each extracted server path, fetch its GitHub page to get the description:

```
web_fetch: https://github.com/{owner}/{repo}
prompt: Return the repository description (one sentence shown under the repo name) and the
        repository's full URL.
```

Fetch all candidates in parallel if possible. If a fetch fails, skip that candidate.

## Step 3 — Filter by relevance

From the fetched candidates, select those relevant to the user's request. Relevance criteria:
- The server's name or description directly addresses the requested capability
- Partial matches are acceptable (e.g. "Elasticsearch" matches "full-text search")
- Exclude servers clearly unrelated to the request

Return ALL relevant candidates — do not limit to one. If no candidates match, return an empty list.

## Step 4 — Return result

Finish with a `mcp_candidate_list` artifact. For each candidate include:
- `name`: the repo path as shown in the registry (e.g. `github/github-mcp-server`)
- `repo_url`: the full GitHub URL (e.g. `https://github.com/github/github-mcp-server`)
- `description`: the repository's one-line description
