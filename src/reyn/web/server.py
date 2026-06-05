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

    FP-0009 Component B + FP-0041 #489 PR-B: dispatches based on job
    shape via ``build_default_runner``.

      - Skill-based legacy: resolves the skill by name and runs through
        ``Agent.run`` with sane defaults from ``load_config()``.
      - Message-based (FP-0041): pushes message into target agent's
        inbox via ``AgentRegistry.ensure_running``, with
        ``sender="cron:<name>"`` envelope so the agent reads it as a
        normal attributed turn from a scheduled trigger.
    """
    from reyn.cron.runners import build_default_runner

    async def _legacy_skill_runner(job) -> str:
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

        # #997 dir2: config-derived permission/runtime bundle via from_config
        # (the web cron path is non-interactive → interactive=False, matching the
        # prior hand-built resolver). resolver now derives from cfg.models (was an
        # empty ModelResolver() default) so class-name model resolution works.
        agent = Agent.from_config(
            cfg,
            interactive=False,
            project_context=project_context,
            caller="cron",
        )

        result = await agent.run(skill, dict(job.input))
        return "ok" if result.ok else "error"

    async def _inbox_pusher(to: str, envelope: dict) -> str:
        """Deliver ``envelope`` to agent ``to`` via the registry.

        Uses ``ensure_running`` so the agent's router loop is live to
        consume the inbox put — same pattern A2A uses. The envelope
        is dispatched as ``kind="user"`` so the LLM processes the
        text as a turn; PR-A sender attribution emits the
        ``[context shift]`` state_change entry from the
        ``sender="cron:<name>"`` field.
        """
        from reyn.web.deps import _get_registry
        registry = _get_registry()
        try:
            session = await registry.ensure_running(to)
        except FileNotFoundError:
            return "error"
        await session._put_inbox("user", dict(envelope))
        return "ok"

    return build_default_runner(
        legacy_skill_runner=_legacy_skill_runner,
        inbox_pusher=_inbox_pusher,
    )


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
                schedule=j.schedule,
                to=j.to,
                message=j.message,
                skill=j.skill,
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
            # FP-0041 #489 PR-B2: expose the scheduler to LLM-callable
            # cron tools (= cron__register / unregister / enable /
            # disable) so they can apply live updates without restart.
            from reyn.cron import set_active_scheduler
            set_active_scheduler(scheduler)
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
        # Clear the active-scheduler registry so LLM-callable tools
        # invoked after shutdown don't dispatch to a stopped scheduler.
        from reyn.cron import set_active_scheduler
        set_active_scheduler(None)


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
from reyn.web.routers import resources as _resources_router  # noqa: E402
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

# FP-0041 #489 plugin framework: webhook plugins (= sample_slack / external
# packages) are activated by ``webhooks.yaml`` (= dedicated file at the
# project root, separate from reyn.yaml to keep core config lean) and
# mounted by the plugin loader at app startup. Reyn core stays SDK-free;
# per-transport protocol code lives in plugins.
try:
    from reyn.config import _find_project_root as _find_root_for_plugins
    from reyn.web.plugin_loader import load_webhook_plugins, load_webhooks_yaml
    _project_root_for_plugins = _find_root_for_plugins(Path.cwd()) or Path.cwd()
    _webhooks_cfg = load_webhooks_yaml(_project_root_for_plugins)
    load_webhook_plugins(app=app, webhooks_config=_webhooks_cfg)
except Exception as _exc:  # noqa: BLE001 — defensive boot
    logger.warning("webhook plugin loading failed: %s", _exc)

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

# #385 β core impl sub-task 3: HTTP fetch surface for path_ref bodies
# (= GET /agents/<agent>/tool-results/<artifact>). Cross-host consumers
# (= A2A peers, MCP `resources/read` adapter, browser, curl) resolve a
# resource_uri / url to a body via plain HTTP GET — fills the A2A
# spec gap for resource fetch with the industry-standard pattern.
app.include_router(_resources_router.router)

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
