"""Tier 2: FP-0041 #489 PR-B2 — LLM-callable cron tool surface.

5 ToolDefinitions (= ``cron__register / unregister / list / enable /
disable``) registered under the ``cron`` action category. Each
mutating handler:
  - persists to ``.reyn/cron.yaml`` (= #470 invariant align,
    runtime-mutable)
  - applies live update to the active CronScheduler (= when one is
    registered via ``set_active_scheduler``)
  - gates via ``PermissionResolver.require_cron_register`` (= per-job
    approval, ``cron_register:<name>`` key)

Pins:

  1. CronScheduler.add_job / remove_job / set_enabled live mutation
     correctness (= idempotency, task lifecycle).
  2. active scheduler registry get/set/clear.
  3. cron_register handler: persist + live add when scheduler active.
  4. cron_register handler: persist-only when no scheduler.
  5. cron_register handler: replace semantics on duplicate name.
  6. cron_unregister handler: remove from file + live.
  7. cron_list handler: prefers live scheduler view; falls back to
     config file when no scheduler.
  8. cron_enable / cron_disable handlers: toggle + file persist.
  9. ToolDefinition registration: 5 tools in default registry.
 10. Permission gate: cron_register key + decl guard.

Tier 2 because the surface is the LLM-callable entry point for the
humanic vision's "register a scheduled message" use case. A
regression that broke persistence or live-update silently degrades
the feature (= UX appears to work but next restart loses state, or
fires don't reflect updates).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.cron import (
    CronJob,
    CronScheduler,
    get_active_scheduler,
    set_active_scheduler,
)
from reyn.tools.cron import (
    CRON_REGISTER,
    _handle_cron_disable,
    _handle_cron_enable,
    _handle_cron_list,
    _handle_cron_register,
    _handle_cron_unregister,
)

# ── Helpers ────────────────────────────────────────────────────────────


class _Workspace:
    """Minimal ToolContext.workspace stub exposing only ``root``."""
    def __init__(self, root: Path):
        self.root = root


class _ToolCtx:
    """Minimal ToolContext stub.

    Only the fields the cron handlers touch are populated. Skips
    permission gate and intervention bus to keep tests focused on the
    storage + scheduler behaviour. The gate path itself is covered
    by a separate test that injects a permission resolver mock.
    """
    def __init__(self, root: Path):
        self.workspace = _Workspace(root)
        self.permission_resolver = None  # _gate short-circuits on None
        self.events = None
        self.phase_state = None
        self.router_state = None
        self.intervention_bus = None


@pytest.fixture(autouse=True)
def _clear_active_scheduler():
    """Clear the module-level active scheduler before/after each test
    so state doesn't leak across tests.
    """
    set_active_scheduler(None)
    yield
    set_active_scheduler(None)


# ── Section 1: CronScheduler live mutation API ────────────────────────


@pytest.mark.asyncio
async def test_add_job_registers_and_can_be_retrieved():
    """Tier 2: ``add_job`` registers the job; ``get_job`` returns it."""
    sched = CronScheduler(jobs=[])
    job = CronJob(name="x", schedule="0 9 * * *", to="agent_x", message="hi")
    await sched.add_job(job)
    assert sched.get_job("x") is job


@pytest.mark.asyncio
async def test_add_job_replaces_existing_by_name():
    """Tier 2: re-adding under the same name replaces. Used by
    ``cron__register`` to swap definitions without restart.
    """
    sched = CronScheduler(jobs=[])
    await sched.add_job(CronJob(name="x", schedule="0 9 * * *", to="a", message="m1"))
    await sched.add_job(CronJob(name="x", schedule="0 10 * * *", to="b", message="m2"))
    assert sched.get_job("x").to == "b"
    assert sched.get_job("x").message == "m2"


@pytest.mark.asyncio
async def test_remove_job_returns_true_when_existed():
    """Tier 2: ``remove_job`` returns True iff the job existed."""
    sched = CronScheduler(jobs=[])
    await sched.add_job(CronJob(name="x", schedule="0 9 * * *", to="a", message="m"))
    assert (await sched.remove_job("x")) is True
    assert sched.get_job("x") is None
    # Idempotent second remove returns False.
    assert (await sched.remove_job("x")) is False


@pytest.mark.asyncio
async def test_set_enabled_toggles_job():
    """Tier 2: ``set_enabled`` flips ``job.enabled`` and returns True
    when the job exists.
    """
    sched = CronScheduler(jobs=[])
    job = CronJob(name="x", schedule="0 9 * * *", to="a", message="m", enabled=True)
    await sched.add_job(job)

    assert (await sched.set_enabled("x", False)) is True
    assert sched.get_job("x").enabled is False

    assert (await sched.set_enabled("x", True)) is True
    assert sched.get_job("x").enabled is True

    # Unknown job returns False.
    assert (await sched.set_enabled("nope", True)) is False


# ── Section 2: active scheduler registry ───────────────────────────────


def test_active_scheduler_default_none():
    assert get_active_scheduler() is None


def test_set_active_scheduler_round_trip():
    sched = CronScheduler(jobs=[])
    set_active_scheduler(sched)
    assert get_active_scheduler() is sched
    set_active_scheduler(None)
    assert get_active_scheduler() is None


# ── Section 3: cron_register handler ──────────────────────────────────


@pytest.mark.asyncio
async def test_cron_register_persists_to_yaml(tmp_path: Path):
    """Tier 2: ``cron_register`` writes the job to ``.reyn/cron.yaml``."""
    ctx = _ToolCtx(tmp_path)
    result = await _handle_cron_register(
        {
            "name": "morning_news",
            "to": "news_agent",
            "message": "今日のまとめ",
            "schedule": "0 9 * * *",
        },
        ctx,
    )
    assert result["status"] == "ok"
    assert result["replaced"] is False

    import yaml
    written = yaml.safe_load((tmp_path / ".reyn" / "cron.yaml").read_text())
    jobs = written["cron"]["jobs"]
    assert len(jobs) == 1
    j = jobs[0]
    assert j["name"] == "morning_news"
    assert j["to"] == "news_agent"
    assert j["message"] == "今日のまとめ"
    assert j["schedule"] == "0 9 * * *"
    assert j["enabled"] is True


@pytest.mark.asyncio
async def test_cron_register_applies_live_update_when_scheduler_active(
    tmp_path: Path,
):
    """Tier 2: with an active scheduler, ``cron_register`` also calls
    ``scheduler.add_job`` so the next fire reflects the change.
    """
    sched = CronScheduler(jobs=[])
    set_active_scheduler(sched)
    ctx = _ToolCtx(tmp_path)

    result = await _handle_cron_register(
        {"name": "x", "to": "a", "message": "m", "schedule": "0 9 * * *"},
        ctx,
    )
    assert result["live_update_applied"] is True
    assert sched.get_job("x") is not None
    assert sched.get_job("x").to == "a"


@pytest.mark.asyncio
async def test_cron_register_persist_only_when_no_scheduler(tmp_path: Path):
    """Tier 2: with no active scheduler, ``cron_register`` still
    persists to the yaml file but reports ``live_update_applied=False``.
    Operator can re-boot ``reyn web`` to pick up the change.
    """
    assert get_active_scheduler() is None
    ctx = _ToolCtx(tmp_path)
    result = await _handle_cron_register(
        {"name": "x", "to": "a", "message": "m", "schedule": "0 9 * * *"},
        ctx,
    )
    assert result["live_update_applied"] is False
    assert (tmp_path / ".reyn" / "cron.yaml").exists()


@pytest.mark.asyncio
async def test_cron_register_replaces_existing(tmp_path: Path):
    """Tier 2: re-registering with the same name replaces the
    existing entry in both yaml and live scheduler.
    """
    sched = CronScheduler(jobs=[])
    set_active_scheduler(sched)
    ctx = _ToolCtx(tmp_path)

    await _handle_cron_register(
        {"name": "x", "to": "a", "message": "old", "schedule": "0 9 * * *"},
        ctx,
    )
    result = await _handle_cron_register(
        {"name": "x", "to": "b", "message": "new", "schedule": "0 10 * * *"},
        ctx,
    )
    assert result["replaced"] is True

    import yaml
    written = yaml.safe_load((tmp_path / ".reyn" / "cron.yaml").read_text())
    jobs = written["cron"]["jobs"]
    assert len(jobs) == 1  # not duplicated
    assert jobs[0]["to"] == "b"
    assert jobs[0]["message"] == "new"

    assert sched.get_job("x").to == "b"


# ── Section 4: cron_unregister handler ─────────────────────────────────


@pytest.mark.asyncio
async def test_cron_unregister_removes_from_yaml(tmp_path: Path):
    """Tier 2: ``cron_unregister`` removes the entry from
    ``.reyn/cron.yaml`` and reports removed=True.
    """
    ctx = _ToolCtx(tmp_path)
    await _handle_cron_register(
        {"name": "x", "to": "a", "message": "m", "schedule": "0 9 * * *"},
        ctx,
    )
    result = await _handle_cron_unregister({"name": "x"}, ctx)
    assert result["removed"] is True

    import yaml
    written = yaml.safe_load((tmp_path / ".reyn" / "cron.yaml").read_text())
    assert written["cron"]["jobs"] == []


@pytest.mark.asyncio
async def test_cron_unregister_unknown_is_noop(tmp_path: Path):
    """Tier 2: removing a non-existent job is a no-op (removed=False)
    without crashing.
    """
    ctx = _ToolCtx(tmp_path)
    result = await _handle_cron_unregister({"name": "ghost"}, ctx)
    assert result["removed"] is False


@pytest.mark.asyncio
async def test_cron_unregister_applies_live_removal(tmp_path: Path):
    """Tier 2: with active scheduler, unregister also calls
    ``scheduler.remove_job``.
    """
    sched = CronScheduler(jobs=[])
    set_active_scheduler(sched)
    ctx = _ToolCtx(tmp_path)

    await _handle_cron_register(
        {"name": "x", "to": "a", "message": "m", "schedule": "0 9 * * *"},
        ctx,
    )
    assert sched.get_job("x") is not None

    await _handle_cron_unregister({"name": "x"}, ctx)
    assert sched.get_job("x") is None


# ── Section 5: cron_list handler ──────────────────────────────────────


@pytest.mark.asyncio
async def test_cron_list_prefers_live_scheduler_view(tmp_path: Path):
    """Tier 2: when a scheduler is active, ``cron_list`` returns the
    live ``scheduler.jobs()`` view (= includes last_run_* runtime
    fields).
    """
    sched = CronScheduler(jobs=[
        CronJob(name="job1", schedule="0 9 * * *", to="a", message="m1"),
        CronJob(name="job2", schedule="0 10 * * *", to="b", message="m2"),
    ])
    set_active_scheduler(sched)
    ctx = _ToolCtx(tmp_path)

    result = await _handle_cron_list({}, ctx)
    assert result["status"] == "ok"
    assert result["source"] == "live_scheduler"
    names = sorted(j["name"] for j in result["jobs"])
    assert names == ["job1", "job2"]


@pytest.mark.asyncio
async def test_cron_list_falls_back_to_config_when_no_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: with no active scheduler, ``cron_list`` reads from
    config files (= .reyn/cron.yaml + reyn.yaml union).
    """
    # Plant config files so load_config has something to read.
    (tmp_path / "reyn.yaml").write_text(
        "model: standard\n", encoding="utf-8",
    )
    (tmp_path / ".reyn").mkdir(exist_ok=True)
    (tmp_path / ".reyn" / "cron.yaml").write_text(
        "cron:\n  jobs:\n"
        "    - name: a_job\n      to: alpha\n      message: hi\n"
        "      schedule: '0 9 * * *'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "reyn.config._find_project_root", lambda _cwd: tmp_path,
    )
    monkeypatch.chdir(tmp_path)

    ctx = _ToolCtx(tmp_path)
    result = await _handle_cron_list({}, ctx)
    assert result["status"] == "ok"
    assert result["source"] == "config_file"
    names = [j["name"] for j in result["jobs"]]
    assert "a_job" in names


# ── Section 6: cron_enable / cron_disable handlers ────────────────────


@pytest.mark.asyncio
async def test_cron_enable_persists_and_live_updates(tmp_path: Path):
    """Tier 2: ``cron_enable`` flips ``enabled=true`` in yaml + live
    scheduler.
    """
    sched = CronScheduler(jobs=[])
    set_active_scheduler(sched)
    ctx = _ToolCtx(tmp_path)

    await _handle_cron_register(
        {
            "name": "x", "to": "a", "message": "m",
            "schedule": "0 9 * * *", "enabled": False,
        },
        ctx,
    )
    assert sched.get_job("x").enabled is False

    result = await _handle_cron_enable({"name": "x"}, ctx)
    assert result["enabled"] is True
    assert sched.get_job("x").enabled is True

    import yaml
    written = yaml.safe_load((tmp_path / ".reyn" / "cron.yaml").read_text())
    assert written["cron"]["jobs"][0]["enabled"] is True


@pytest.mark.asyncio
async def test_cron_disable_persists_and_live_updates(tmp_path: Path):
    """Tier 2: ``cron_disable`` flips ``enabled=false`` in yaml + live
    scheduler.
    """
    sched = CronScheduler(jobs=[])
    set_active_scheduler(sched)
    ctx = _ToolCtx(tmp_path)

    await _handle_cron_register(
        {"name": "x", "to": "a", "message": "m", "schedule": "0 9 * * *"},
        ctx,
    )
    assert sched.get_job("x").enabled is True

    result = await _handle_cron_disable({"name": "x"}, ctx)
    assert result["enabled"] is False
    assert sched.get_job("x").enabled is False


# ── Section 7: ToolDefinition registration ────────────────────────────


def test_cron_tools_registered_in_default_registry():
    """Tier 2: the 5 cron tools are in the default registry under
    category="cron" with the expected gate matrix.
    """
    from reyn.tools import get_default_registry
    reg = get_default_registry()
    cron_tools = {t.name: t for t in reg if t.category == "cron"}
    assert set(cron_tools.keys()) == {
        "cron_register", "cron_unregister", "cron_list",
        "cron_enable", "cron_disable",
    }
    # Gate matrix: register/unregister/enable/disable are router-only;
    # list is dual-surface (read-only).
    assert cron_tools["cron_register"].gates.router == "allow"
    assert cron_tools["cron_register"].gates.phase == "deny"
    assert cron_tools["cron_list"].gates.router == "allow"
    assert cron_tools["cron_list"].gates.phase == "allow"


def test_cron_register_parameters_require_name_to_message_schedule():
    """Tier 2: the JSON Schema enforces the 4 required fields
    (= name / to / message / schedule). ``enabled`` is optional.
    """
    params = CRON_REGISTER.parameters
    assert params["required"] == ["name", "to", "message", "schedule"]
    props = params["properties"]
    assert "name" in props
    assert "to" in props
    assert "message" in props
    assert "schedule" in props
    assert "enabled" in props


# ── Section 8: permission gate ────────────────────────────────────────


@pytest.mark.asyncio
async def test_require_cron_register_raises_without_decl(tmp_path: Path):
    """Tier 2: ``require_cron_register`` raises PermissionError when
    the PermissionDecl doesn't declare ``cron_register=True``.
    """
    from reyn.permissions.permissions import PermissionDecl, PermissionResolver
    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=False,
    )
    decl = PermissionDecl(cron_register=False)

    with pytest.raises(PermissionError):
        await resolver.require_cron_register(decl, "any_job", bus=None)


@pytest.mark.asyncio
async def test_require_cron_register_passes_when_config_allows(tmp_path: Path):
    """Tier 2: with ``permissions.cron_register: allow`` in config
    + decl declared, ``require_cron_register`` returns without
    prompting.
    """
    from reyn.permissions.permissions import PermissionDecl, PermissionResolver
    resolver = PermissionResolver(
        config_permissions={"cron_register": "allow"},
        project_root=tmp_path,
        interactive=False,
    )
    decl = PermissionDecl(cron_register=True)

    # No bus needed because config allows = early return.
    await resolver.require_cron_register(decl, "any_job", bus=None)


@pytest.mark.asyncio
async def test_require_cron_register_persists_approval_under_per_job_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: when the auto-approve env var is set, the approval key
    is ``cron_register:<job_name>`` so different jobs require
    independent approval.
    """
    from reyn.permissions.permissions import PermissionDecl, PermissionResolver
    monkeypatch.setenv("REYN_CRON_REGISTER_AUTO_APPROVE", "1")
    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=False,
    )
    decl = PermissionDecl(cron_register=True)

    await resolver.require_cron_register(decl, "morning_news", bus=None)
    assert resolver._saved.get("cron_register:morning_news") is True
    # Different job not auto-approved.
    assert "cron_register:other" not in resolver._saved


def test_permission_decl_from_dict_parses_cron_register():
    """Tier 2: ``PermissionDecl.from_dict`` reads the ``cron_register``
    key from agent profile / skill frontmatter.
    """
    from reyn.permissions.permissions import PermissionDecl
    decl = PermissionDecl.from_dict({"cron_register": True})
    assert decl.cron_register is True
    decl2 = PermissionDecl.from_dict({})
    assert decl2.cron_register is False
