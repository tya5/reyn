"""FastAPI application entry point for the Reyn web gateway.

Mounts all REST routers and WebSocket routes. CORS is configured for
localhost development; tighten `allow_origins` before exposing to the
network.

Static mounts (PR30 — OpenUI shell):
    /                        → shell index.html (redirect to /static/index.html)
    /static/{path}           → src/reyn/web/openui/static/
    /web/designs/{slug}/{path} → design files from three roots (project → local → stdlib)

Per P7: this module contains no skill-specific strings. All engine data
passes through as opaque JSON payloads.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(
    title="Reyn Web Gateway",
    description=(
        "Thin HTTP + WebSocket gateway wrapping the Reyn agent engine. "
        "App surface and Studio surface share the same API; the frontend "
        "decides which vocabulary to expose."
    ),
    version="0.1.0",
)

# ── CORS: localhost only (dev). Tighten before production. ──────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8080",
        "http://127.0.0.1",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:8080",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── mount REST routers ──────────────────────────────────────────────────────

from reyn.web.routers import a2a as _a2a_router  # noqa: E402
from reyn.web.routers import agents as _agents_router  # noqa: E402
from reyn.web.routers import budget as _budget_router  # noqa: E402
from reyn.web.routers import mcp as _mcp_router  # noqa: E402
from reyn.web.routers import permissions as _perms_router  # noqa: E402
from reyn.web.routers import runs as _runs_router  # noqa: E402
from reyn.web.routers import skills as _skills_router  # noqa: E402
from reyn.web.routers import topologies as _topos_router  # noqa: E402
from reyn.web.routers import web_config as _web_config_router  # noqa: E402
from reyn.web.routers import web_data as _web_data_router  # noqa: E402

app.include_router(_agents_router.router, prefix="/api")
app.include_router(_skills_router.router, prefix="/api")
app.include_router(_runs_router.router, prefix="/api")
app.include_router(_topos_router.router, prefix="/api")
app.include_router(_perms_router.router, prefix="/api")
app.include_router(_budget_router.router, prefix="/api")
app.include_router(_web_config_router.router, prefix="/api")
app.include_router(_web_data_router.router, prefix="/api")

# MCP-over-SSE: GET /mcp/sse (event stream) + POST /mcp/messages (client → server).
# The router carries the GET; the POST endpoint is a Starlette Mount because
# SseServerTransport.handle_post_message is itself an ASGI app.
# Mounting is best-effort: skip if `[mcp]` extra isn't installed so the rest
# of the gateway still boots.
app.include_router(_mcp_router.router)
try:
    app.router.routes.append(_mcp_router.get_mcp_message_mount())
except ImportError:  # pragma: no cover — `[mcp]` extra not installed
    import logging as _logging
    _logging.getLogger(__name__).info(
        "mcp SDK not installed; /mcp/messages POST endpoint disabled. "
        "Install with `pip install -e .[mcp]` to enable MCP-over-SSE."
    )

# A2A (Agent2Agent) protocol: peer agents discover Reyn agents via
# Agent Cards at GET /a2a/agents/<name>/.well-known/agent-card.json
# and converse via JSON-RPC 2.0 POST /a2a/agents/<name>. Same backing
# impl as MCP (send_to_agent_impl), different wire protocol.
app.include_router(_a2a_router.router)

# A2A async task lifecycle registry: process-singleton attached to
# app.state so that get_run_registry(request) can retrieve it.
from reyn.web.run_registry import RunRegistry  # noqa: E402
app.state.run_registry = RunRegistry()


# ── WebSocket routes ────────────────────────────────────────────────────────

from reyn.web.ws import chat as _ws_chat  # noqa: E402

app.include_router(_ws_chat.router)


# ── health check ────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {"status": "ok"}


# ── static: OpenUI shell ─────────────────────────────────────────────────────

_STATIC_DIR = Path(__file__).parent / "openui" / "static"

# Serve /static/* from the shell's static directory.
# Mount only if directory exists (avoids startup error in stripped installs).
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="openui_static")


@app.get("/", tags=["shell"], include_in_schema=False)
async def shell_root() -> RedirectResponse:
    """Redirect / → /static/index.html (the OpenUI shell entry point)."""
    return RedirectResponse(url="/static/index.html", status_code=302)


# ── design file serving ───────────────────────────────────────────────────────
#
# Serves design assets from three roots: project → local → stdlib (web/designs/).
# The project root is determined from the env or from the runtime default.


def _get_project_root_path() -> Path:
    """Resolve project root for design serving.

    Uses the same logic as deps.get_project_root but without FastAPI Depends
    so it can be called from a plain route handler.
    """
    from reyn.web.deps import _get_project_root
    return _get_project_root()


@app.get("/web/designs/{slug}/{file_path:path}", tags=["shell"], include_in_schema=False)
async def serve_design_file(slug: str, file_path: str) -> FileResponse:
    """Serve a file from the first matching design root (project > local > stdlib)."""
    from fastapi import HTTPException

    project_root = _get_project_root_path()
    roots = [
        project_root / "reyn" / "project" / "designs",
        project_root / "reyn" / "local"   / "designs",
        project_root / "web"  / "designs",
    ]

    for root in roots:
        candidate = root / slug / file_path
        if candidate.is_file():
            return FileResponse(str(candidate))

    raise HTTPException(status_code=404, detail=f"Design file not found: {slug}/{file_path}")


__all__ = ["app"]
