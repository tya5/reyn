"""Cron ToolDefinitions — FP-0041 #489 PR-B2.

LLM-callable surface for the cron message-based shape landed in PR-B
(= ``to + message`` jobs dispatched to target agent's inbox with
``sender="cron:<name>"``). Five action-category entries:

  CRON_REGISTER   — add/replace a cron job (purity=side_effect)
  CRON_UNREGISTER — remove a cron job (purity=side_effect)
  CRON_LIST       — list current jobs (purity=read_only)
  CRON_ENABLE     — toggle a job to enabled (purity=side_effect)
  CRON_DISABLE    — toggle a job to disabled (purity=side_effect)

LLM call shape (= invoke_action wrapper category):

  invoke_action(action_name="cron__register", args={
      "name": "morning_news",
      "to": "news_agent",
      "message": "今日のニュースまとめ",
      "schedule": "0 9 * * *",
      "enabled": true,
  })

Persistence + live update:

  All mutating handlers persist to ``.reyn/cron.yaml`` (= #470 invariant
  align, runtime-mutable). When a live ``CronScheduler`` is registered
  via ``set_active_scheduler``, the handler also calls
  ``add_job`` / ``remove_job`` / ``set_enabled`` so the next fire reflects
  the change without restart. When no live scheduler exists (= CLI
  subcommand context, or scheduler not yet booted), the .reyn/cron.yaml
  write still happens; the next ``reyn web`` boot loads it.

Permission gating:

  ``cron_register`` permission key (= shared across register / unregister
  / enable / disable). Skill must declare ``cron_register: true`` AND
  per-job approval is collected via ``PermissionResolver.require_cron_register``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# ── Description literals ──────────────────────────────────────────────

_CRON_REGISTER_DESCRIPTION = (
    "Schedule a recurring message to a Reyn agent. The cron scheduler "
    "delivers the message to the target agent's inbox at each cron "
    "fire — the agent processes it as a normal attributed turn from "
    "a scheduled trigger. Idempotent on `name` (= replaces existing). "
    "Use for periodic checks, reminders, automated summaries."
)

_CRON_UNREGISTER_DESCRIPTION = (
    "Remove a previously-registered cron job by name. The schedule "
    "stops firing immediately. No-op if the job doesn't exist."
)

_CRON_LIST_DESCRIPTION = (
    "List all currently-registered cron jobs (= both reyn.yaml legacy "
    "and .reyn/cron.yaml dynamic entries, unioned). Returns job name, "
    "target, message/skill, schedule, enabled state, and next-run time."
)

_CRON_ENABLE_DESCRIPTION = (
    "Enable a previously-disabled cron job. The scheduler resumes "
    "firing it on its schedule. No-op if already enabled."
)

_CRON_DISABLE_DESCRIPTION = (
    "Disable a cron job without removing it. The schedule stops firing "
    "until re-enabled via `cron__enable`. Use to pause a job temporarily."
)


# ── Parameter schemas ─────────────────────────────────────────────────

_CRON_NAME_PARAM = {
    "name": {
        "type": "string",
        "description": (
            "Unique job identifier within the project (e.g. "
            "'morning_news', 'weekly_report'). Reused across "
            "register/unregister/enable/disable."
        ),
    },
}

_CRON_REGISTER_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_CRON_NAME_PARAM,
        "to": {
            "type": "string",
            "description": (
                "Target Reyn agent name. The scheduled message is "
                "delivered to this agent's inbox; the agent must "
                "exist in the project."
            ),
        },
        "message": {
            "type": "string",
            "description": (
                "Free-form text dispatched to the agent. Treated as a "
                "user-turn-shaped message with sender='cron:<name>'."
            ),
        },
        "schedule": {
            "type": "string",
            "description": (
                "5-field cron expression (e.g. '0 9 * * *' = daily 9am, "
                "'0 */6 * * *' = every 6 hours, '0 9 * * MON' = Mondays "
                "9am)."
            ),
        },
        "enabled": {
            "type": "boolean",
            "description": (
                "Whether the schedule fires immediately. Defaults to "
                "true. Set false to register a paused job and enable "
                "later via cron__enable."
            ),
        },
    },
    "required": ["name", "to", "message", "schedule"],
}

_CRON_NAME_ONLY_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": _CRON_NAME_PARAM,
    "required": ["name"],
}

_CRON_LIST_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {},
}


# ── Storage helpers (= .reyn/cron.yaml read/write) ────────────────────


def _dynamic_cron_yaml_path(ctx: ToolContext) -> Path:
    """Resolve the path to ``.reyn/cron.yaml`` under the project root.

    Falls back to ``Path.cwd() / .reyn / cron.yaml`` when the workspace
    doesn't expose a root attribute (= defensive against test stubs).
    """
    root = getattr(ctx.workspace, "root", None) or getattr(
        ctx.workspace, "base_dir", None,
    )
    if root is None:
        root = Path.cwd()
    return Path(root) / ".reyn" / "cron.yaml"


def _read_dynamic_cron(path: Path) -> dict:
    """Read ``.reyn/cron.yaml`` (or empty dict when absent / malformed)."""
    if not path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_dynamic_cron(path: Path, data: dict) -> None:
    """Write ``data`` as YAML to ``.reyn/cron.yaml``, creating parents."""
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(
            data, allow_unicode=True, default_flow_style=False, sort_keys=False,
        ),
        encoding="utf-8",
    )


def _jobs_list(data: dict) -> list:
    """Extract the cron.jobs list from a parsed yaml dict, defensive."""
    cron = data.get("cron", {})
    if not isinstance(cron, dict):
        return []
    jobs = cron.get("jobs", [])
    return list(jobs) if isinstance(jobs, list) else []


def _set_jobs_list(data: dict, jobs: list) -> dict:
    """Return a copy of ``data`` with ``cron.jobs`` set to ``jobs``."""
    new = dict(data)
    cron = dict(new.get("cron", {})) if isinstance(new.get("cron"), dict) else {}
    cron["jobs"] = jobs
    new["cron"] = cron
    return new


# ── Permission helper ────────────────────────────────────────────────


async def _gate(ctx: ToolContext, job_name: str) -> None:
    """Permission gate for cron mutation tools (#571 collapse arc Phase 5).

    The bool-axis ``require_cron_register`` per-job approval prompt was
    removed in Phase 5. Authorisation is now layered:

    - The calling skill must list this tool in ``permissions.tool`` so
      the LLM is permitted to invoke it (= operator authorisation
      happens at skill-startup time via ``require_tool``).
    - The runtime gate here is the standard ``require_file_write``
      against the canonical ``.reyn/cron.yaml`` path. Since the tool
      is OS-internal (= no per-tool skill frontmatter), we synthesise
      a minimal PermissionDecl listing the canonical path explicitly
      and route it through ``session_approve_path`` once per resolver
      instance so subsequent calls pass silently.

    No-op in unit-test contexts (= ``ctx.permission_resolver`` is None).
    """
    from reyn.security.permissions.permissions import PermissionDecl
    if ctx.permission_resolver is None:
        return
    cron_yaml_path = str(_dynamic_cron_yaml_path(ctx))
    decl = PermissionDecl(file_write=[{"path": cron_yaml_path, "scope": "just_path"}])
    # OS-internal tool: session-approve the canonical path so the
    # require_file_write check passes without an interactive prompt.
    # The TOOL-level authorisation already happened at skill-startup
    # time (= require_tool against the calling skill's permissions.tool).
    ctx.permission_resolver.session_approve_path(
        cron_yaml_path, "cron", "file.write",
    )
    await ctx.permission_resolver.require_file_write(decl, cron_yaml_path, "cron")


# ── Handlers ─────────────────────────────────────────────────────────


async def _handle_cron_register(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Register or replace a cron job."""
    name = str(args["name"])
    to = str(args["to"])
    message = str(args["message"])
    schedule = str(args["schedule"])
    enabled = bool(args.get("enabled", True))

    await _gate(ctx, name)

    # Persist to .reyn/cron.yaml.
    path = _dynamic_cron_yaml_path(ctx)
    data = _read_dynamic_cron(path)
    jobs = _jobs_list(data)
    new_entry = {
        "name": name,
        "to": to,
        "message": message,
        "schedule": schedule,
        "enabled": enabled,
    }
    replaced = False
    out_jobs = []
    for j in jobs:
        if isinstance(j, dict) and j.get("name") == name:
            out_jobs.append(new_entry)
            replaced = True
        else:
            out_jobs.append(j)
    if not replaced:
        out_jobs.append(new_entry)
    _write_dynamic_cron(path, _set_jobs_list(data, out_jobs))

    # Live update if a scheduler is registered.
    from reyn.cron import CronJob, get_active_scheduler
    sched = get_active_scheduler()
    if sched is not None:
        await sched.add_job(CronJob(
            name=name, schedule=schedule, to=to, message=message,
            enabled=enabled,
        ))

    return {
        "status": "ok",
        "name": name,
        "replaced": replaced,
        "live_update_applied": sched is not None,
        "path": str(path),
    }


async def _handle_cron_unregister(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Remove a cron job by name."""
    name = str(args["name"])
    await _gate(ctx, name)

    path = _dynamic_cron_yaml_path(ctx)
    data = _read_dynamic_cron(path)
    jobs = _jobs_list(data)
    out_jobs = [
        j for j in jobs
        if not (isinstance(j, dict) and j.get("name") == name)
    ]
    removed = len(out_jobs) < len(jobs)
    if removed:
        _write_dynamic_cron(path, _set_jobs_list(data, out_jobs))

    from reyn.cron import get_active_scheduler
    sched = get_active_scheduler()
    live_removed = False
    if sched is not None:
        live_removed = await sched.remove_job(name)

    return {
        "status": "ok",
        "name": name,
        "removed": removed or live_removed,
        "live_update_applied": sched is not None,
        "path": str(path),
    }


async def _handle_cron_list(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """List current cron jobs.

    Prefers the live scheduler's view when registered (= includes
    last_run_* runtime fields). Falls back to file-only read of
    ``.reyn/cron.yaml`` + reyn.yaml legacy union when no scheduler is
    active (= e.g. invoked at boot before scheduler is up, or in
    test).
    """
    _ = args  # noqa: ARG001 — list takes no args
    from reyn.cron import get_active_scheduler
    sched = get_active_scheduler()
    if sched is not None:
        rows = [j.to_dict() for j in sched.jobs()]
        return {
            "status": "ok",
            "source": "live_scheduler",
            "jobs": rows,
        }

    # Fallback: read .reyn/cron.yaml + reyn.yaml cron.jobs union via
    # config.load_config (= same path the scheduler would use on boot).
    try:
        from reyn.config import load_config
        cfg = load_config()
        rows = [
            {
                "name": j.name,
                "to": j.to,
                "message": j.message,
                "skill": j.skill,
                "schedule": j.schedule,
                "input": dict(j.input),
                "enabled": j.enabled,
            }
            for j in cfg.cron.jobs
        ]
        return {
            "status": "ok",
            "source": "config_file",
            "jobs": rows,
        }
    except Exception as exc:
        return {
            "status": "error",
            "source": "config_file",
            "error": f"{type(exc).__name__}: {exc}",
            "jobs": [],
        }


async def _set_enabled(
    args: Mapping[str, Any], ctx: ToolContext, *, enabled: bool,
) -> ToolResult:
    """Shared backbone for cron__enable / cron__disable."""
    name = str(args["name"])
    await _gate(ctx, name)

    path = _dynamic_cron_yaml_path(ctx)
    data = _read_dynamic_cron(path)
    jobs = _jobs_list(data)
    found = False
    out_jobs = []
    for j in jobs:
        if isinstance(j, dict) and j.get("name") == name:
            new_j = dict(j)
            new_j["enabled"] = enabled
            out_jobs.append(new_j)
            found = True
        else:
            out_jobs.append(j)
    if found:
        _write_dynamic_cron(path, _set_jobs_list(data, out_jobs))

    from reyn.cron import get_active_scheduler
    sched = get_active_scheduler()
    live_applied = False
    if sched is not None:
        live_applied = await sched.set_enabled(name, enabled)

    return {
        "status": "ok",
        "name": name,
        "enabled": enabled,
        "found_in_dynamic": found,
        "live_update_applied": sched is not None and live_applied,
    }


async def _handle_cron_enable(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    return await _set_enabled(args, ctx, enabled=True)


async def _handle_cron_disable(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    return await _set_enabled(args, ctx, enabled=False)


# ── ToolDefinition instances ─────────────────────────────────────────


CRON_REGISTER = ToolDefinition(
    name="cron_register",
    description=_CRON_REGISTER_DESCRIPTION,
    parameters=_CRON_REGISTER_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_cron_register,
    category="cron",
    purity="side_effect",
)

CRON_UNREGISTER = ToolDefinition(
    name="cron_unregister",
    description=_CRON_UNREGISTER_DESCRIPTION,
    parameters=_CRON_NAME_ONLY_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_cron_unregister,
    category="cron",
    purity="side_effect",
)

CRON_LIST = ToolDefinition(
    name="cron_list",
    description=_CRON_LIST_DESCRIPTION,
    parameters=_CRON_LIST_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_cron_list,
    category="cron",
    purity="read_only",
)

CRON_ENABLE = ToolDefinition(
    name="cron_enable",
    description=_CRON_ENABLE_DESCRIPTION,
    parameters=_CRON_NAME_ONLY_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_cron_enable,
    category="cron",
    purity="side_effect",
)

CRON_DISABLE = ToolDefinition(
    name="cron_disable",
    description=_CRON_DISABLE_DESCRIPTION,
    parameters=_CRON_NAME_ONLY_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_cron_disable,
    category="cron",
    purity="side_effect",
)
