"""FastAPI application entry point for the Reyn web gateway.

Mounts all REST routers and WebSocket routes. CORS is configured for
localhost development; tighten `allow_origins` before exposing to the
network.

Per P7: this module contains no skill-specific strings. All engine data
passes through as opaque JSON payloads.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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

from reyn.web.routers import agents as _agents_router  # noqa: E402
from reyn.web.routers import skills as _skills_router  # noqa: E402
from reyn.web.routers import runs as _runs_router      # noqa: E402
from reyn.web.routers import topologies as _topos_router  # noqa: E402
from reyn.web.routers import permissions as _perms_router  # noqa: E402
from reyn.web.routers import budget as _budget_router  # noqa: E402

app.include_router(_agents_router.router, prefix="/api")
app.include_router(_skills_router.router, prefix="/api")
app.include_router(_runs_router.router, prefix="/api")
app.include_router(_topos_router.router, prefix="/api")
app.include_router(_perms_router.router, prefix="/api")
app.include_router(_budget_router.router, prefix="/api")


# ── WebSocket routes ────────────────────────────────────────────────────────

from reyn.web.ws import chat as _ws_chat  # noqa: E402

app.include_router(_ws_chat.router)


# ── health check ────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {"status": "ok"}


__all__ = ["app"]
