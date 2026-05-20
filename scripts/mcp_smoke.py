#!/usr/bin/env python3
"""Direct-call MCP smoke test runner.

Bypasses the skill / agent layer. Loads reyn.local.yaml's mcp.servers
entry by name, opens an MCPClient, lists tools, and (optionally) calls
one tool with the given args. Useful for confirming server install +
basic connectivity without engaging permissions / skill dispatch.

Usage:

    python scripts/mcp_smoke.py <server-name>
        # → lists tools

    python scripts/mcp_smoke.py <server-name> <tool> <json-args>
        # → calls tool with args, prints result

Example:

    python scripts/mcp_smoke.py memory read_graph '{}'
    python scripts/mcp_smoke.py time get_current_time '{"timezone": "Asia/Tokyo"}'

issue #318 / #319 workaround note: when calling this script against a
freshly-installed server, edit reyn.local.yaml to (a) add `type: stdio`
and (b) rename the auto-generated `server-foo` key to a stable short
name. See issues for repro details.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import yaml


async def _run() -> int:
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2

    server_name = argv[0]
    tool_name = argv[1] if len(argv) > 1 else None
    args_json = argv[2] if len(argv) > 2 else "{}"

    config_path = Path("reyn.local.yaml")
    if not config_path.exists():
        print(f"error: {config_path} not found in CWD", file=sys.stderr)
        return 2

    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    servers = (cfg or {}).get("mcp", {}).get("servers", {})
    if server_name not in servers:
        print(f"error: server {server_name!r} not in reyn.local.yaml", file=sys.stderr)
        print(f"available: {sorted(servers)}", file=sys.stderr)
        return 2

    from reyn.mcp_client import MCPClient

    client = MCPClient(servers[server_name])
    try:
        await client.initialize()
        if tool_name is None:
            tools = await client.list_tools()
            for t in tools:
                print(f"- {t.get('name')}: {(t.get('description') or '').splitlines()[0][:90]}")
            return 0
        try:
            args = json.loads(args_json)
        except json.JSONDecodeError as exc:
            print(f"error: invalid json args: {exc}", file=sys.stderr)
            return 2
        result = await client.call_tool(tool_name, args)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0
    finally:
        await client.close()


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
