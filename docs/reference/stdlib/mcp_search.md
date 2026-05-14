---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [mcp_search]
---

# `mcp_search`

Search the MCP registry for servers matching a natural-language capability description.

## Entry

`search`

## Final output

`mcp_candidate_list` — `candidates[]` with `name`, `repo_url`, `description` (verbatim from registry, never paraphrased), and `source` field (`"registry"` / `"registry_stale"` / `"error"`).

## How it composes

A single `search` phase. The `registry_fetch.py` preprocessor (unsafe mode, 20 s timeout) queries the registry; the LLM filters by relevance and finishes — no Control IR ops are emitted. If the registry is unreachable but a local cache exists, `source` is `"registry_stale"`. If no cache is available, `source` is `"error"` and `candidates` is empty.

## Caveats

Anthropic's official reference servers (GitHub, filesystem, memory) are not listed in the registry. If the user asks for them, direct them to `https://github.com/orgs/modelcontextprotocol/repositories`. The caller is responsible for selection when multiple candidates are returned.

## Usage

```bash
reyn run mcp_search "GitHub リポジトリの操作"
reyn run mcp_search "database query tools"
```

## Source

[`src/reyn/stdlib/skills/mcp_search/skill.md`](https://github.com/tya5/reyn/blob/main/src/reyn/stdlib/skills/mcp_search/skill.md)
