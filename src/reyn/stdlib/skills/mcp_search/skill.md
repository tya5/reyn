---
type: skill
name: mcp_search
description: Search the MCP registry for servers relevant to a natural-language capability request
entry: search
final_output: mcp_candidate_list
final_output_description: |
  List of MCP server candidates from registry.modelcontextprotocol.io that match the requested capability.
  Empty list if no relevant servers are found.
finish_criteria:
  - The MCP registry has been queried (via preprocessor)
  - Candidates have been filtered by relevance to the user's request
  - Result list is returned (may be empty)
graph:
  search: []
permissions:
  python:
    - module: ./registry_fetch.py
      function: fetch_registry_results
      mode: trusted
      timeout: 20
routing:
  intents: [task]
  when_to_use:
    - User wants to find / discover MCP servers for some capability
    - User asks for MCP server recommendations matching an integration need
  when_not_to_use:
    - User asks conceptually what MCP is (stable_knowledge)
    - User asks to use a *specific known* MCP server (configure directly)
  examples:
    positive:
      - "Slack 連携できる MCP サーバーを探して"
      - "GitHub の MCP サーバーが欲しい"
      - "Find MCP servers for Notion"
    negative:
      - "MCP って何？"
      - "MCP サーバーの設定方法は？"
---

## Overview

Queries the official MCP registry at `registry.modelcontextprotocol.io` and returns servers
relevant to the requested capability. Results come from a deterministic preprocessor; the LLM
only filters and presents them. Caller is responsible for selection when multiple candidates are
returned.

Note: Anthropic's official reference servers (e.g. `modelcontextprotocol/server-github`) are not
yet listed in the registry. For these, the fallback is to search GitHub directly at
`https://github.com/orgs/modelcontextprotocol/repositories`.

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
