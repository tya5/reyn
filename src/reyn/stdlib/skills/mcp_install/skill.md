---
type: skill
name: mcp_install
description: Install an MCP server from the registry into the current project configuration
entry: discover
final_output: mcp_install_result
final_output_description: |
  Result of the MCP server installation attempt. Includes status, server_id,
  server_name (short config key), scope, and installed_path.
finish_criteria:
  - The user's installation request has been interpreted
  - A server has been selected (either directly or after presenting candidates)
  - The mcp_install op has been emitted to install the server
  - Installation result has been confirmed
graph:
  discover: []
permissions:
  mcp_install: true
  python:
    - module: ./registry_fetch.py
      function: fetch_server_for_install
      mode: unsafe
      timeout: 20
# FP-0016 D: this skill needs no static secrets / OAuth tokens.
required_credentials: []
routing:
  intents: [task]
  when_to_use:
    - User wants to install an MCP server
    - User says "MCP server をインストールして" or similar
    - User references a specific MCP server by name and wants to add it to the project
  when_not_to_use:
    - User only wants to search / discover MCP servers (use mcp_search instead)
    - User asks conceptually what MCP is
    - User wants to call/use a server that is already installed
  examples:
    positive:
      - "filesystem の MCP server をインストールして"
      - "io.github.modelcontextprotocol/server-filesystem を入れて"
      - "GitHub MCP server をプロジェクトに追加して"
    negative:
      - "MCP サーバーを探して"
      - "MCP って何？"
      - "既存の MCP server を使って"
---

## Overview

Installs an MCP server from `registry.modelcontextprotocol.io` into the project's
configuration. The preprocessor fetches the server's metadata; the LLM selects the
target server and scope, then emits a `mcp_install` Control IR op to perform the
actual installation.

The install op handler (OS layer) owns: registry fetch, runtime check, permission
gate, secret credential prompting, config file writing, and event emission.

## Input

Natural language install request or explicit server_id:

```
reyn run mcp_install "filesystem MCP server を入れて"
reyn run mcp_install "io.github.modelcontextprotocol/server-filesystem"
```

## Output

`mcp_install_result` with `status`, `server_id`, `server_name`, `scope`, and
`installed_path`. Status is `"ok"` on success, `"error"` on failure, or
`"skipped"` if permission was denied.
