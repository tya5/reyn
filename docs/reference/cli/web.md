---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn web]
---

# `reyn web`

Start the Reyn FastAPI + WebSocket gateway server.

## Synopsis

```
reyn web [OPTIONS]
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--host HOST` | `127.0.0.1` | Interface to bind. Use `0.0.0.0` for LAN access. |
| `--port PORT` | `8080` | TCP port. |
| `--reload` | off | Hot-reload on source changes (development only). |
| `--log-level LEVEL` | `info` | `critical` / `error` / `warning` / `info` / `debug` / `trace`. |
| `--default-design SLUG` | unset | Sets `REYN_WEB_DEFAULT_DESIGN` for the OpenUI shell. |

## Requirements

```bash
pip install "reyn[web]"
```

Exits 1 with an install hint if FastAPI or Uvicorn is missing.

## Endpoints

| Path | Protocol |
|------|----------|
| `/ws/chat` | WebSocket chat |
| `/a2a/agents` | A2A agent discovery |
| `/a2a/agents/<name>` | A2A JSON-RPC 2.0 per agent |
| `/mcp/sse`, `/mcp/messages` | MCP-over-SSE |
| `/api/*` | REST (agents / skills / runs / budget / permissions) |

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Server stopped cleanly (Ctrl+C). |
| `1` | Missing extras or bind error. |

## Examples

```bash
reyn web
reyn web --port 9000 --log-level debug
reyn web --host 0.0.0.0 --port 8080
```
