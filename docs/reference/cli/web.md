---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn web]
---

# `reyn web`

Start the Reyn FastAPI gateway server (HTTP + SSE).

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
| `--enable SURFACE` | unset | Opt a surface in (repeatable — `--enable a2a --enable mcp`). Surfaces: `agui` / `webui` / `health` / `api` / `resources` (secure-default ON), `a2a` / `mcp` (secure-default OFF). |
| `--disable SURFACE` | unset | Opt a surface out (repeatable). Precedence: `--enable`/`--disable` > `web.surfaces` config > secure-default — see [reyn.yaml § web.surfaces](../config/reyn-yaml.md#websurfaces--per-surface-opt-inopt-out-fp-0058-p2). |

## Requirements

```bash
pip install "reyn[web]"
```

Exits 1 with an install hint if FastAPI or Uvicorn is missing.

## Endpoints

| Path | Protocol |
|------|----------|
| `/agui/chat/<name>/events` | AG-UI chat stream over SSE (server→client) — the single UI transport for the local CUI, the remote thin client, and the openui browser |
| `/agui/chat/<name>` | AG-UI turn submit / HITL answer / cancel / seize / heartbeat (client→server POST) |
| `/a2a/agents` | A2A agent discovery — **opt-in**, `--enable a2a` |
| `/a2a/agents/<name>` | A2A JSON-RPC 2.0 per agent — **opt-in**, `--enable a2a` |
| `/mcp/sse`, `/mcp/messages` | MCP-over-SSE — **opt-in**, `--enable mcp` |
| `/api/*` | REST (agents / skills / runs / budget / permissions) |
| `/agents/<name>/tool-results/<artifact>` | HTTP fetch for `path_ref` bodies (resources) |

A2A and MCP are broad machine-integration ports (peer agents / external LLM
clients reaching into this process) and are **off by default** — every other
surface above is on by default. See [`--enable`/`--disable`](#options) and
[reyn.yaml § web.surfaces](../config/reyn-yaml.md#websurfaces--per-surface-opt-inopt-out-fp-0058-p2).

## Authentication

**Every functional surface is authenticated by the same transport-tier model** —
not just the AG-UI chat routes. A request to `/api/*`, `/a2a/*`, `/mcp/*`, or a
resource-fetch route is resolved to a connection identity before it reaches the
handler; an unauthenticated request is refused with `401`. Authentication is
uniform across the surfaces (one operator token), so the same token that drives
the browser / thin client also authorizes the REST control plane and the A2A /
MCP surfaces.

The tier determines what is required:

- **Same-machine UDS** (`--uds PATH`) — identified by OS peer credentials; no
  token needed.
- **Loopback / network TCP** — the bearer token is required, presented as
  `?token=<secret>` or an `Authorization: Bearer <secret>` header. A non-loopback
  bind refuses to start without `web.auth.token` configured (fail-closed); a
  loopback bind generates an ephemeral token at startup and prints it in the
  launch URL.

Open (unauthenticated) surfaces are only the non-sensitive ones: the OpenUI
shell assets (`/`, `/static/*`, `/web/designs/*`) — the browser loads them
*before* it has the token, then supplies the token on the API calls it makes —
and `/health`. Webhook plugin routes (`/webhook/*`) do their own HMAC
verification and are not double-gated. A CORS preflight (`OPTIONS`) is answered
without a token.

See [reyn.yaml § web.auth](../config/reyn-yaml.md) for the token / TLS /
transport-tier configuration and [AG-UI transport](../runtime/agui-transport.md)
for the chat surface's per-handler details.

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
reyn web --enable a2a --enable mcp   # opt in to the machine-integration surfaces
reyn web --disable api               # turn off a surface that's on by default
```
