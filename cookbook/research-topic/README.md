# research-topic

> 🔮 **Roadmap example.** Depends on: a generic-web-search MCP server you've
> configured under `mcp:` in `reyn.yaml`, plus a `web_research` stdlib skill
> that wraps it. Not runnable on Reyn v1 as of 2026-05-02.
>
> Tracked in: post-OSS roadmap. Note: the `mcp_search` stdlib skill that
> ships today is a **registry search** (it queries `github.com/mcp` for MCP
> *servers* matching a capability). It is **not** a generic web-research
> entry point — calling it with "What changed in DuckDB v1.0?" returns
> server candidates, not research findings. This recipe documents the
> intended end-to-end shape; the wiring still has to be built.

Run web research on a topic and get a structured summary back.

## What this shows

- Driving a stdlib skill with natural language input.
- How a skill that needs an external capability (web search) is wired
  through MCP rather than baked into the OS (P7).

## Prerequisites

- An MCP search server configured in `reyn.yaml` under `mcp:`. The default
  recipe assumes a server named `search` exposing a `web_search` tool. If
  you don't have one, the run will fail fast at the first `mcp_call` op —
  fix `reyn.yaml` and retry.

## Run it

```bash
reyn run mcp_search "What changed in DuckDB v1.0 vs v0.10?"
```

Or with explicit JSON input:

```bash
reyn run mcp_search '{"type":"user_message","data":{"text":"DuckDB v1.0 changes"}}'
```

## Expected output

A `final_output` artifact with the synthesized findings plus citations to
the URLs the search returned. Token / cost summary follows.

See `transcript.example.txt` for a sketch.

## Variations

- **Different model**: `reyn run mcp_search "..." --model openai/gpt-4o-mini`
- **Output in Japanese**: `reyn run mcp_search "..." --output-language ja`
- **Inspect events**: append `--events` to dump the full event log after.

## Troubleshooting

- `mcp server 'search' not configured` — add a `mcp:` block to `reyn.yaml`.
  See `docs/en/how-to/` for an MCP setup walk-through.
- Empty search results — your MCP server's `web_search` tool may need an
  API key in its own env.

## See also

- [stdlib/mcp_search](../../src/reyn/stdlib/skills/mcp_search/skill.md)
- [How-to: compose skills with run_skill](../../docs/en/how-to/compose-skills-with-run-skill.md)
