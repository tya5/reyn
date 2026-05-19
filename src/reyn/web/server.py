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

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)


def _make_cron_runner():
    """Return an async callable that executes a CronJob headlessly.

    Resolves the skill by name (project → local → stdlib lookup) and runs
    it through Agent.run with sane defaults drawn from load_config().

    TODO: wire full headless-run options (shell_allowed, output_language,
    permission_resolver, mcp_servers, etc.) mirroring cli/commands/run.py
    once cron-specific config surface is defined (FP-0009 follow-up).
    """
    async def _runner(job) -> str:
        from pathlib import Path as _Path

        from reyn.agent import Agent
        from reyn.compiler import load_dsl_skill
        from reyn.config import _find_project_root, load_config, load_project_context
        from reyn.permissions.permissions import PermissionResolver
        from reyn.skill.skill_paths import resolve_skill_path

        cfg = load_config()
        project_root = _find_project_root(_Path.cwd())
        project_context = load_project_context(cfg, project_root)

        # resolve_skill_path returns (skill_dir, skill_root); load_dsl_skill
        # takes the skill.md path (= skill_dir / "skill.md").
        skill_dir, skill_root = resolve_skill_path(job.skill)
        skill = load_dsl_skill(skill_dir / "skill.md", skill_root=skill_root)

        perm_resolver = PermissionResolver(
            config_permissions=dict(cfg.permissions),
            project_root=project_root,
            interactive=False,
        )

        agent = Agent(
            model=cfg.model,
            permission_resolver=perm_resolver,
            mcp_servers=cfg.mcp,
            python_allowed_modules=list(cfg.python.allowed_modules),
            prompt_cache_enabled=cfg.prompt_cache_enabled,
            project_context=project_context,
            caller="cron",
            sandbox_config=cfg.sandbox,
        )

        result = await agent.run(skill, dict(job.input))
        return "ok" if result.ok else "error"

    return _runner


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # ── Startup ──

    # FP-0001 + issue #267 Gap 5: RunRegistry singleton — process-wide
    # task lifecycle tracking with snapshot persistence so a process
    # restart preserves A2A async-task state (= the structural gap that
    # left A2A peer routing half-restored against the ChatSession-side
    # outstanding_interventions persistence wired by PR-intervention-link
    # L2-L6). Snapshot path follows the convention established by
    # ChatSession's per-agent snapshot.json: server-level state lives at
    # ``.reyn/state/run_registry.json``.
    from pathlib import Path  # noqa: PLC0415

    from reyn.web.run_registry import RunRegistry
    persist_path = Path(".reyn") / "state" / "run_registry.json"
    app.state.run_registry = RunRegistry(persist_path=persist_path)

    # FP-0009 B: cron scheduler — start only if reyn.yaml has any enabled
    # cron jobs.  Failures are caught so a misconfigured cron block does
    # not prevent the web gateway from booting.
    app.state.cron_scheduler = None  # default — overwritten below if needed
    try:
        from reyn.config import load_config
        from reyn.cron import CronJob, CronScheduler
        cfg = load_config()
        cron_jobs = [
            CronJob(
                name=j.name,
                skill=j.skill,
                schedule=j.schedule,
                input=dict(j.input),
                enabled=j.enabled,
            )
            for j in cfg.cron.jobs
        ]
        if any(j.enabled for j in cron_jobs):
            scheduler = CronScheduler(cron_jobs)
            scheduler.set_runner(_make_cron_runner())
            await scheduler.start()
            app.state.cron_scheduler = scheduler
            logger.info(
                "Started cron scheduler with %d enabled job(s)",
                sum(1 for j in cron_jobs if j.enabled),
            )
    except Exception as exc:  # noqa: BLE001 — defensive boot
        logger.warning(
            "Cron scheduler failed to start: %s. Continuing without scheduler.", exc
        )

    yield  # ── App runs ──

    # ── Shutdown ──
    sched = getattr(app.state, "cron_scheduler", None)
    if sched is not None:
        try:
            await sched.stop()
        except Exception as exc:  # noqa: BLE001 — defensive shutdown
            logger.warning("Cron scheduler stop failed: %s", exc)


app = FastAPI(
    title="Reyn Web Gateway",
    description=(
        "Thin HTTP + WebSocket gateway wrapping the Reyn agent engine. "
        "App surface and Studio surface share the same API; the frontend "
        "decides which vocabulary to expose."
    ),
    version="0.1.0",
    lifespan=_lifespan,
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
