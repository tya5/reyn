"""FastAPI application entry point for the Reyn web gateway.

The app object + lifespan (cron / A2A run-registry / task backend /
disposition sweep) live here; CORS + the P1 auth gate are installed here.
Every hosted **surface** — AG-UI, the OpenUI web shell (`/`, `/static/*`,
`/web/designs/*`), `/health`, the REST `/api` control plane, the
resource-fetch routes, A2A, MCP — is mounted through the FP-0058 P2
:mod:`reyn.interfaces.web.surfaces` ``SurfaceSpec`` registry
(``mount_all``), which resolves each surface's opt-in/opt-out posture
(CLI ``--enable``/``--disable`` > ``web.surfaces`` config > secure-default)
before mounting it. See that module for the secure-default table and the
per-surface mount functions. The webhook plugin surface (FP-0041) is
mounted separately, unchanged, via its own ``webhooks.yaml`` opt-in.

CORS is configured for localhost development; tighten `allow_origins`
before exposing to the network.

Per P7: this module contains no domain-specific strings. All engine data
passes through as opaque JSON payloads.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)


def _make_cron_runner():
    """Return an async callable that executes a CronJob headlessly.

    FP-0009 Component B + FP-0041 #489 PR-B: dispatches based on job
    shape via ``build_default_runner``. Task-based job execution uses the
    message-based path; only message-based jobs run:

      - Message-based (FP-0041): pushes message into target agent's
        inbox via ``AgentRegistry.ensure_running``, with
        ``sender="cron:<name>"`` envelope so the agent reads it as a
        normal attributed turn from a scheduled trigger.
    """
    from reyn.runtime.cron.runners import build_default_runner

    async def _inbox_pusher(to: str, envelope: dict, native_id: str) -> str:
        """Deliver ``envelope`` to the job's own ``cron:<job_name>`` Session of
        agent ``to`` via the registry.

        FP-0043 S4b-3a: a message-based cron job is re-keyed from the agent's
        "main" session to a per-job ``cron:<native_id>`` Session (resolve_session
        get-or-spawn). Persistent per job — the stable ``native_id`` (= job name)
        resumes the SAME session across fires, so the conversation accumulates a
        history of prior runs ("what changed since last run"). The run-loop is
        booted WITHOUT a forwarder (``ensure_session_running``) since cron is
        unattended (output handling = S4b-3b notify layer); the envelope is
        dispatched as ``kind="user"`` so the LLM processes the text as a turn, with
        ``sender="cron:<name>"`` driving the PR-A attribution state_change.
        """
        from reyn.interfaces.web.deps import _get_registry
        from reyn.runtime.cron.routing import dispatch_cron_fired, resolve_cron_session
        registry = _get_registry()
        try:
            session = resolve_cron_session(registry, to, native_id)
        except (FileNotFoundError, KeyError):
            return "error"
        # #2608 H5: fire the cron_fired external-event hook on the job's own
        # resolved session — non-blocking, so a slow hook action never stalls
        # this fire's own inbox delivery below. See dispatch_cron_fired's
        # docstring.
        dispatch_cron_fired(session, native_id, to)
        payload = dict(envelope)
        # FP-0043 S4b-3b: opt-in notify → tag the inbox with reply_to=ExternalRef so
        # the agent's final reply is relayed to the channel by the (already
        # factory-wired) external-transport outbox interceptor. No notify → no
        # reply_to → interceptor falls through → event-log only (current behaviour).
        notify = payload.pop("notify", None)
        if notify:
            from reyn.runtime.transport import ExternalRef
            payload["reply_to"] = ExternalRef(transport=notify, destination={})
        await session._put_inbox("user", payload)
        return "ok"

    async def _failure_notifier(job, reason: str) -> None:
        """FP-0043 S4b-3b errors=(b): relay a job-execution FAILURE (a job that
        never produced a reply) to its notify channel, via the SAME MCP route the
        outbox interceptor uses (telegram→broker__post_message). Best-effort —
        unconfigured channel / unresolvable session degrades to event-log."""
        from reyn.config import load_config
        from reyn.interfaces.web.deps import _get_registry
        from reyn.runtime.cron.routing import resolve_cron_session
        from reyn.runtime.external_routing import make_session_mcp_dispatcher, route_to_mcp
        routing = load_config().external_transports
        if not routing.transports:
            return
        registry = _get_registry()
        try:
            session = resolve_cron_session(registry, job.to, job.name)
        except (FileNotFoundError, KeyError):
            return
        await route_to_mcp(
            job.notify, {},
            f"⚠ cron job {job.name!r} failed: {reason}",
            routing=routing,
            mcp_dispatcher=make_session_mcp_dispatcher(session),
        )

    return build_default_runner(
        inbox_pusher=_inbox_pusher,
        failure_notifier=_failure_notifier,
    )


# #1953 slice 5a-2: how often the disposition sweep runs (wall-clock interval;
# decoupled from inbound A2A traffic for §24 forward-progress).
_DISPOSITION_SWEEP_INTERVAL_SECONDS = 30.0


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # ── Startup ──

    # Durable asyncio unhandled-exception capture (see
    # reyn.core.events.asyncio_diagnostics). `reyn web` is uvicorn-owned —
    # uvicorn.run() creates and owns the loop, so there is no
    # asyncio.run()-call-site choke point to hook (unlike `run_async` for
    # the CLI chat path). The lifespan startup hook is the first point this
    # process has a *running* loop to install the handler on, and it runs
    # exactly once per server process regardless of how many browser/API
    # clients subsequently attach.
    import asyncio  # noqa: PLC0415

    from reyn.core.events.asyncio_diagnostics import (  # noqa: PLC0415
        install_asyncio_exception_handler,
    )
    install_asyncio_exception_handler(asyncio.get_running_loop())

    # ── Server-side authentication context (ADR-0039 P0) ──────────────────────
    # Built once per process; read on every AG-UI SSE connection to gate the
    # answer / permission-grant paths. The effective token is handed in from the
    # CLI via REYN_WEB_AUTH_TOKEN (or web.auth.token); when neither is set a
    # token is generated so the surface is never left unauthenticated — the
    # generated value is logged so a direct-uvicorn launch can still connect.
    # Defensive boot (mirrors the cron block below): a config-load failure must
    # not prevent the gateway from booting, but the auth gate MUST still be
    # present — so fall back to an env/generated token rather than leaving
    # app.state.auth unset.
    from reyn.interfaces.web.auth import AuthContext  # noqa: PLC0415
    try:
        from reyn.config import load_config  # noqa: PLC0415
        app.state.auth = AuthContext.from_env_and_config(load_config())
    except Exception as exc:  # noqa: BLE001 — defensive boot; the gate must exist
        app.state.auth = AuthContext.from_env_and_config(None)
        logger.warning(
            "web.auth: config load failed (%s); using an env/generated token so "
            "the auth gate is still enforced.", exc,
        )
    if getattr(app.state.auth, "token_was_generated", False):
        logger.warning(
            "web.auth: no token configured; generated an ephemeral gateway "
            "token for this run: %s", app.state.auth.token,
        )

    # FP-0001 + issue #267 Gap 5: RunRegistry singleton — process-wide
    # task lifecycle tracking with snapshot persistence so a process
    # restart preserves A2A async-task state (= the structural gap that
    # left A2A peer routing half-restored against the Session-side
    # outstanding_interventions persistence wired by PR-intervention-link
    # L2-L6). Snapshot path follows the convention established by
    # Session's per-agent snapshot.json: server-level state lives at
    # ``.reyn/state/run_registry.json``.
    from pathlib import Path  # noqa: PLC0415

    from reyn.interfaces.web.run_registry import RunRegistry
    persist_path = Path(".reyn") / "state" / "run_registry.json"
    app.state.run_registry = RunRegistry(persist_path=persist_path)

    # #1953 slice 5a: the process-singleton Task backend the A2A surface reads
    # (GetTask / ListTasks / Cancel). Single store keyed by session_id columns
    # (the §24/R1 per-session store for rewind is revisited at slice 9). Durable
    # sqlite under the server state dir.
    from reyn.task import create_task_backend  # noqa: PLC0415
    task_db_path = Path(".reyn") / "state" / "tasks.db"
    app.state.task_backend = create_task_backend("sqlite", path=str(task_db_path))

    # #1953 slice 5a-2: A2A-owned webhook registry (P7 — webhook_url stays out of
    # the core Task model) + a periodic disposition sweep. The sweep is
    # backend-derived + decoupled from inbound traffic (§24 forward-progress) and
    # runs as a lifespan-owned asyncio task (mirrors the cron_scheduler lifecycle).
    import asyncio  # noqa: PLC0415

    from reyn.interfaces.web.a2a_webhook_registry import (  # noqa: PLC0415
        A2AWebhookRegistry,
        sweep_dispositions,
    )
    app.state.a2a_webhook_registry = A2AWebhookRegistry(
        persist_path=Path(".reyn") / "state" / "a2a_webhooks.json"
    )

    async def _disposition_sweep_loop() -> None:
        while True:
            try:
                await sweep_dispositions(
                    app.state.task_backend, app.state.a2a_webhook_registry
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a sweep error must not kill the loop
                logger.warning("disposition sweep pass failed: %s", exc)
            await asyncio.sleep(_DISPOSITION_SWEEP_INTERVAL_SECONDS)

    app.state.disposition_sweep_task = asyncio.create_task(_disposition_sweep_loop())

    # FP-0009 B: cron scheduler — start only if reyn.yaml has any enabled
    # cron jobs.  Failures are caught so a misconfigured cron block does
    # not prevent the web gateway from booting.
    app.state.cron_scheduler = None  # default — overwritten below if needed
    try:
        from reyn.config import load_config
        from reyn.runtime.cron import CronJob, CronScheduler
        cfg = load_config()
        cron_jobs = [
            CronJob(
                name=j.name,
                schedule=j.schedule,
                to=j.to,
                message=j.message,
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
            from reyn.runtime.cron import set_active_scheduler
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
    # #1953 slice 5a-2: cancel the disposition sweep loop (no leak — await it so
    # the cancellation propagates before the process exits).
    sweep_task = getattr(app.state, "disposition_sweep_task", None)
    if sweep_task is not None:
        sweep_task.cancel()
        try:
            await sweep_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — defensive shutdown
            pass

    sched = getattr(app.state, "cron_scheduler", None)
    if sched is not None:
        try:
            await sched.stop()
        except Exception as exc:  # noqa: BLE001 — defensive shutdown
            logger.warning("Cron scheduler stop failed: %s", exc)
        # Clear the active-scheduler registry so LLM-callable tools
        # invoked after shutdown don't dispatch to a stopped scheduler.
        from reyn.runtime.cron import set_active_scheduler
        set_active_scheduler(None)

    # #1953 slice 5a: close the sqlite task backend opened at startup. Without
    # this the sqlite connection (and its WAL lock on ``.reyn/state/tasks.db``)
    # is leaked on every lifespan shutdown — the module-level ``app`` singleton
    # overwrites ``app.state.task_backend`` on the next startup and the orphaned
    # connection lingers until GC. Because the backend opens with
    # ``PRAGMA busy_timeout=0`` (deliberate fail-fast — a lock contention raises
    # immediately rather than waiting), an un-GC'd orphan overlapping the next
    # open surfaces as ``sqlite3.OperationalError: database is locked``. Under a
    # TestClient-per-test suite this is order-dependent flake; in production it
    # is a genuine fd/lock leak across gateway restarts. Closing here mirrors
    # the sweep-task cancel + cron-scheduler stop above (defensive: never let a
    # shutdown error mask the exit).
    task_backend = getattr(app.state, "task_backend", None)
    if task_backend is not None:
        try:
            task_backend.close()
        except Exception as exc:  # noqa: BLE001 — defensive shutdown
            logger.warning("Task backend close failed: %s", exc)


app = FastAPI(
    title="Reyn Web Gateway",
    description=(
        "Thin HTTP + AG-UI SSE gateway wrapping the Reyn agent engine. "
        "App surface and Studio surface share the same API; the frontend "
        "decides which vocabulary to expose."
    ),
    version="0.1.0",
    lifespan=_lifespan,
)

# ── Auth gate (ADR-0039 P1): mount-front authentication for the non-AG-UI
# surfaces (/api, /a2a, /mcp, resources). Reuses the P0 auth substrate; adds no
# new auth. Added BEFORE the CORS middleware so CORS stays OUTERMOST (Starlette
# prepends, so last-added wraps first-added): a CORS preflight OPTIONS is
# answered without a token, and only then does the auth gate see the request.
from reyn.interfaces.web.auth_gate import AuthGateMiddleware  # noqa: E402

app.add_middleware(AuthGateMiddleware)

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


# ── mount surfaces (FP-0058 P2: SurfaceSpec registry) ───────────────────────
#
# FP-0041 #489 plugin framework: webhook plugins (= sample_slack / external
# packages) are activated by ``webhooks.yaml`` (= dedicated file at the
# project root, separate from reyn.yaml to keep core config lean) and
# mounted by the plugin loader at app startup. Reyn core stays SDK-free;
# per-transport protocol code lives in plugins. This surface stays outside
# the SurfaceSpec registry below — it is unrelated to the core secure-default
# table and keeps its own existing opt-in (``webhooks.yaml`` per-plugin
# ``enabled:``), unchanged by FP-0058.
app.state.gateway_tools = []
try:
    from reyn.config import _find_project_root as _find_root_for_plugins
    from reyn.interfaces.web.plugin_loader import (
        load_webhook_plugins,
        load_webhook_tools,
        load_webhooks_yaml,
    )
    _project_root_for_plugins = _find_root_for_plugins(Path.cwd()) or Path.cwd()
    _webhooks_cfg = load_webhooks_yaml(_project_root_for_plugins)
    load_webhook_plugins(app=app, webhooks_config=_webhooks_cfg)
    # #1805: collect each plugin's outbound MCP tools (register_tools) so the
    # in-process MCP server (handle_sse → build_server) hosts them — a complete
    # gateway plugin = inbound webhook + outbound tool in one process.
    app.state.gateway_tools = load_webhook_tools(webhooks_config=_webhooks_cfg)
except Exception as _exc:  # noqa: BLE001 — defensive boot
    logger.warning("webhook plugin loading failed: %s", _exc)

# Core surfaces (AG-UI / OpenUI web shell / health / REST /api / resources /
# A2A / MCP): each resolved enabled/disabled (CLI --enable/--disable >
# web.surfaces config > secure-default) and mounted via the SAME
# mount(app, config) -> APIRouter | None seam the plugin loader above already
# used — see reyn.interfaces.web.surfaces for the registry, the secure-default
# table, and the strip-gate falsification note.
from reyn.interfaces.web.surfaces import mount_all  # noqa: E402

try:
    from reyn.config import load_config as _load_config_for_surfaces
    _surfaces_config = _load_config_for_surfaces()
except Exception as _exc:  # noqa: BLE001 — defensive boot
    logger.warning(
        "config load failed while resolving web surfaces (%s); "
        "falling back to the secure-default table.", _exc,
    )
    _surfaces_config = None

mount_all(app, _surfaces_config)


__all__ = ["app"]
