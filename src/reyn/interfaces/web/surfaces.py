"""SurfaceSpec registry — FP-0058 Phase 2: opt-in/opt-out mount selection.

Before this module, ``server.py`` mounted every core surface (AG-UI, the
OpenUI web shell, ``/health``, the REST ``/api`` control plane, the resource-
fetch routes, A2A, MCP) **unconditionally** via ``app.include_router(...)``,
while the FP-0041 webhook *plugin* loader (:mod:`reyn.interfaces.web.plugin_loader`)
already mounted its routers conditionally — a plugin's
``register_router(config) -> APIRouter | None`` returns ``None`` to opt out,
and the loader gates each plugin's mount on ``enabled: true/false`` in
``webhooks.yaml``. That divergence (core = always-on, plugins = opt-in) is
what this module resolves: every core surface now goes through the SAME
enabled-check → ``mount(app, config) -> APIRouter | None`` → conditional-
``include_router`` seam plugins already used. ``None`` is a graceful skip
(a surface can decline to mount itself — e.g. an optional dependency being
absent — exactly like a plugin opting out), not an error.

## Secure-default table (owner-locked, FP-0058 P2)

    ON  (default_enabled=True):  agui, webui, health, api, resources
    OFF (default_enabled=False, opt-in): a2a, mcp

Grounding: least-exposure by default is standard operator-facing-service
practice — Prometheus ships every ``--web.enable-*`` flag OFF by default,
Kubernetes' ``--runtime-config`` opts alpha/experimental APIs in explicitly,
and Jupyter server extensions are enabled per-extension rather than
wildcard-on. A2A and MCP are broad machine-integration ports (peer agents /
external LLM clients reaching into this process); AG-UI/WebUI/REST/health/
resources are the surfaces the local operator's own browser and CLI need to
function at all, so they stay on. The webhook plugin surface is unrelated to
this table — it keeps its existing ``webhooks.yaml``-driven opt-in, unchanged
by this module.

## Precedence

``--enable <surface>`` / ``--disable <surface>`` CLI flags (propagated via the
``REYN_WEB_ENABLE_SURFACES`` / ``REYN_WEB_DISABLE_SURFACES`` comma-separated
env vars, mirroring the existing ``REYN_WEB_*`` CLI→server propagation
pattern used by ``--default-design`` / ``--eager-embedding-build``) beat the
``web.surfaces.<name>.enabled`` config, which beats the secure-default table
above::

    CLI (--enable/--disable) > web.surfaces config > secure-default

Resolution happens once, at server-module-import time (``mount_all`` is
called from ``server.py`` at import, matching where the surfaces were
mounted before this change) — the LLM has no launch authority over which
surfaces exist (protect-at-use, per ``permission-model.md``): only the
operator's CLI flags / config file / built-in default decide, never a runtime
LLM action.

## Strip-gate falsification

``mount_all`` only calls a spec's ``mount()`` when the surface resolves
enabled. Deleting that ``if not enabled: continue`` guard makes ``mount()``
run for every surface unconditionally, so a disabled surface (e.g. A2A on a
fresh install) gets mounted anyway — the reachability tests in
``tests/web/test_surface_registry.py`` that assert a disabled surface 404s
go RED. That is the proof the enabled-check, not something else, is what
keeps an opt-in surface unreachable.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from fastapi import APIRouter, FastAPI

from reyn.config.media import SurfacesConfig

if TYPE_CHECKING:
    from reyn.config.root import ReynConfig

logger = logging.getLogger(__name__)

# ── env-var propagation (mirrors REYN_WEB_DEFAULT_DESIGN / REYN_WEB_EAGER_EMBEDDING_BUILD) ──

ENABLE_SURFACES_ENV_VAR = "REYN_WEB_ENABLE_SURFACES"
DISABLE_SURFACES_ENV_VAR = "REYN_WEB_DISABLE_SURFACES"

MountFn = Callable[[FastAPI, "ReynConfig | None"], Optional[APIRouter]]


@dataclass(frozen=True)
class SurfaceSpec:
    """One hosted surface's registration: name, its mount function, the
    secure-default posture, and its auth identity policy (informational —
    mirrors :func:`reyn.interfaces.web.auth_gate.surface_class_for`; the gate
    itself still decides enforcement, this is not a second enforcement path).
    """
    name: str
    mount: MountFn
    default_enabled: bool
    identity_policy: str  # "open" | "self-gated" | "operator" | "peer" | "client" | "resource"


def cli_overrides_from_env() -> tuple[frozenset[str], frozenset[str]]:
    """Read the CLI ``--enable``/``--disable`` overrides propagated via env.

    Returns ``(enable, disable)`` name sets. A name present in both (a
    contradictory ``--enable x --disable x``) resolves to disabled — the
    same "last/strongest wins toward the safer state" choice the caller
    (``reyn.interfaces.cli.commands.web``) already rejects at parse time is
    backstopped here defensively.
    """
    enable = frozenset(
        s.strip() for s in os.environ.get(ENABLE_SURFACES_ENV_VAR, "").split(",") if s.strip()
    )
    disable = frozenset(
        s.strip() for s in os.environ.get(DISABLE_SURFACES_ENV_VAR, "").split(",") if s.strip()
    )
    return enable, disable


def resolve_enabled(
    spec: SurfaceSpec,
    *,
    surfaces_config: SurfacesConfig,
    cli_enable: frozenset[str],
    cli_disable: frozenset[str],
) -> bool:
    """CLI > config > secure-default, per surface."""
    if spec.name in cli_disable:
        return False
    if spec.name in cli_enable:
        return True
    cfg_val = surfaces_config.enabled.get(spec.name)
    if cfg_val is not None:
        return cfg_val
    return spec.default_enabled


# ── mount functions ───────────────────────────────────────────────────────


def _mount_agui(app: FastAPI, config: "ReynConfig | None") -> APIRouter | None:
    """AG-UI thin-client transport (ADR-0039) — self-gated per-handler."""
    from reyn.interfaces.transport.agui.endpoint import router
    return router


def _mount_webui(app: FastAPI, config: "ReynConfig | None") -> APIRouter | None:
    """OpenUI shell: ``/static/*`` assets, ``/`` shell redirect, design files."""
    from fastapi.responses import FileResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles

    static_dir = Path(__file__).parent / "openui" / "static"
    # Mount only if the directory exists (stripped installs boot without it).
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="openui_static")

    router = APIRouter(tags=["shell"])

    @router.get("/", include_in_schema=False)
    async def shell_root() -> RedirectResponse:
        """Redirect / → /static/index.html (the OpenUI shell entry point)."""
        return RedirectResponse(url="/static/index.html", status_code=302)

    @router.get("/web/designs/{slug}/{file_path:path}", include_in_schema=False)
    async def serve_design_file(slug: str, file_path: str) -> FileResponse:
        """Serve a file from the first matching design root (project > local > stdlib)."""
        from fastapi import HTTPException

        from reyn.interfaces.web.deps import _get_project_root
        project_root = _get_project_root()
        roots = [
            project_root / "reyn" / "project" / "designs",
            project_root / "reyn" / "local" / "designs",
            project_root / "web" / "designs",
        ]
        for root in roots:
            candidate = root / slug / file_path
            if candidate.is_file():
                return FileResponse(str(candidate))
        raise HTTPException(status_code=404, detail=f"Design file not found: {slug}/{file_path}")

    return router


def _mount_health(app: FastAPI, config: "ReynConfig | None") -> APIRouter | None:
    router = APIRouter(tags=["meta"])

    @router.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return router


def _mount_api(app: FastAPI, config: "ReynConfig | None") -> APIRouter | None:
    """The REST ``/api`` control plane — auth-gated ``operator`` class (#2837)."""
    from reyn.interfaces.web.routers import agents as _agents_router
    from reyn.interfaces.web.routers import budget as _budget_router
    from reyn.interfaces.web.routers import permissions as _perms_router
    from reyn.interfaces.web.routers import topologies as _topos_router
    from reyn.interfaces.web.routers import web_config as _web_config_router
    from reyn.interfaces.web.routers import web_data as _web_data_router

    combined = APIRouter()
    combined.include_router(_agents_router.router, prefix="/api")
    combined.include_router(_topos_router.router, prefix="/api")
    combined.include_router(_perms_router.router, prefix="/api")
    combined.include_router(_budget_router.router, prefix="/api")
    combined.include_router(_web_config_router.router, prefix="/api")
    combined.include_router(_web_data_router.router, prefix="/api")
    return combined


def _mount_resources(app: FastAPI, config: "ReynConfig | None") -> APIRouter | None:
    """``/agents/<a>/tool-results/<artifact>`` — auth-gated ``resource`` class."""
    from reyn.interfaces.web.routers import resources as _resources_router
    return _resources_router.router


def _mount_a2a(app: FastAPI, config: "ReynConfig | None") -> APIRouter | None:
    """Agent2Agent JSON-RPC surface — OFF by default (broad machine-integration port)."""
    from reyn.interfaces.web.routers import a2a as _a2a_router
    return _a2a_router.router


def _mount_mcp(app: FastAPI, config: "ReynConfig | None") -> APIRouter | None:
    """MCP-over-SSE surface — OFF by default (broad machine-integration port).

    The message-mount append is best-effort (defensive: the mcp SDK ships
    transitively via the core ``fastmcp`` dependency, so ``ImportError`` here
    indicates a broken install, not a normal absent-extra path) — a failure
    there does not prevent the GET /mcp/sse router from mounting.
    """
    from reyn.interfaces.web.routers import mcp as _mcp_router
    try:
        app.router.routes.append(_mcp_router.get_mcp_message_mount())
    except ImportError:  # pragma: no cover — mcp SDK unexpectedly missing
        logger.info(
            "mcp SDK not importable; /mcp/messages POST endpoint disabled. "
            "The mcp SDK ships with the core `fastmcp` dependency — reinstall reyn "
            "(e.g. pip install -e .) to enable MCP-over-SSE."
        )
    return _mcp_router.router


def build_registry() -> tuple[SurfaceSpec, ...]:
    """The core surface registry — order matches the previous ``server.py``
    mount order (does not affect behaviour; FastAPI route matching is
    prefix/path-based, not registration-order-based across these surfaces)."""
    return (
        SurfaceSpec("api", _mount_api, default_enabled=True, identity_policy="operator"),
        SurfaceSpec("mcp", _mount_mcp, default_enabled=False, identity_policy="client"),
        SurfaceSpec("a2a", _mount_a2a, default_enabled=False, identity_policy="peer"),
        SurfaceSpec("resources", _mount_resources, default_enabled=True, identity_policy="resource"),
        SurfaceSpec("agui", _mount_agui, default_enabled=True, identity_policy="self-gated"),
        SurfaceSpec("health", _mount_health, default_enabled=True, identity_policy="open"),
        SurfaceSpec("webui", _mount_webui, default_enabled=True, identity_policy="open"),
    )


def mount_all(app: FastAPI, config: "ReynConfig | None") -> None:
    """Resolve + mount every registry surface onto ``app``.

    For each :class:`SurfaceSpec`: if it resolves **enabled** (CLI > config >
    secure-default), call ``mount(app, config)``; if it returns a router,
    ``app.include_router`` it. A disabled surface's ``mount`` is never
    called — see the strip-gate falsification note in the module docstring.
    """
    cli_enable, cli_disable = cli_overrides_from_env()
    surfaces_config = SurfacesConfig()
    web_cfg = getattr(config, "web", None)
    if web_cfg is not None:
        surfaces_config = getattr(web_cfg, "surfaces", None) or SurfacesConfig()

    for spec in build_registry():
        enabled = resolve_enabled(
            spec, surfaces_config=surfaces_config, cli_enable=cli_enable, cli_disable=cli_disable,
        )
        if not enabled:
            logger.info("web surface %r disabled; skipping mount", spec.name)
            continue
        try:
            router = spec.mount(app, config)
        except Exception as exc:  # noqa: BLE001 — one surface must not break boot
            logger.warning("web surface %r mount() raised: %s; skipping", spec.name, exc)
            continue
        if router is not None:
            app.include_router(router)


__all__ = [
    "DISABLE_SURFACES_ENV_VAR",
    "ENABLE_SURFACES_ENV_VAR",
    "SurfaceSpec",
    "SurfacesConfig",
    "build_registry",
    "cli_overrides_from_env",
    "mount_all",
    "resolve_enabled",
]
