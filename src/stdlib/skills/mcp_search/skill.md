---
type: skill
name: mcp_search
description: Search github.com/mcp for MCP servers relevant to a natural-language capability request
entry: search
final_output: mcp_candidate_list
final_output_description: |
  List of MCP server candidates from the GitHub MCP Registry that match the requested capability.
  Empty list if no relevant servers are found.
finish_criteria:
  - github.com/mcp has been fetched
  - Candidates have been filtered by relevance to the user's request
  - Result list is returned (may be empty)
graph:
  search: []
---

## Overview

Fetches the curated MCP server registry at `github.com/mcp` and returns servers relevant to the
requested capability. Caller is responsible for selection when multiple candidates are returned.

## Input

Natural language description of the capability needed:

```
reyn run mcp_search "GitHub リポジトリの操作"
reyn run mcp_search "web search"
reyn run mcp_search "PostgreSQL database access"
```

## Output

`mcp_candidate_list` with a `candidates` array. Each entry has `name`, `repo_url`, and
`description`. Returns an empty list if no relevant servers are found.
