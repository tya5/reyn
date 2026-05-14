---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [mcp_install]
---

# `mcp_install`

Guided install of an MCP server from the registry into the project configuration.

## Entry

`discover`

## Final output

`mcp_install_result` — `status` (`"installed"` / `"error"` / `"skipped"`), `server_id`, `server_name`, `scope`, `installed_path`.

## How it composes

A single `discover` phase. The `registry_fetch.py` preprocessor (unsafe mode, 20 s timeout) queries the MCP registry and populates `data.registry` before the LLM runs. The LLM reads `data.registry.source` (`direct` / `search` / `not_found` / `error`), optionally calls `ask_user` to resolve ambiguity, then emits one `mcp_install` Control IR op. The OS layer owns the runtime permission check, secret prompting, config writing, and event emission.

## Caveats

Requires `mcp_install: true` in `reyn.yaml` permissions. Scope defaults to `local` (writes to `reyn.local.yaml`). Anthropic reference servers (GitHub, filesystem, memory) are not listed in the registry — provide an explicit server_id for those.

## Usage

```bash
reyn run mcp_install "filesystem MCP server を入れて"
reyn run mcp_install '{"server_id": "modelcontextprotocol/server-github"}'
```

## Source

[`src/reyn/stdlib/skills/mcp_install/skill.md`](https://github.com/tya5/reyn/blob/main/src/reyn/stdlib/skills/mcp_install/skill.md)
