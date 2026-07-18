"""Tier 2: #2761 PR-2 — immediate mid-turn apply for a PURE-ADDITION install.

The hot-reload arc drops the turn-boundary constraint so an install op can USE the
skill/pipeline it just installed within the same execution. The mechanism is a PATH
CONDITION (not a blanket guard — see the #2761 revised §2 contract): a pure addition
(a brand-new name) on a live per-session reloader applies IMMEDIATELY via
``HotReloader.apply_now`` (only the affected seam); a same-name overwrite
(clobber-update — skill/pipeline have no ``remove`` CLI, so re-install is their only
update path) OR no per-session reloader keeps the EXISTING deferred turn-boundary
path. This confines the R7 in-use-replace hazard to the deferred path it already lives
on, and preserves every update workflow.

Three invariant clusters:
  A. ``apply_now`` runs ONLY the seam(s) the install ``source`` affects (not the whole
     reload), immediately, atomically (validate-before-apply), and independent of the
     deferred ``pending`` schedule.
  B. ``is_pure_addition`` + ``dispatch_install_reload`` route addition→immediate,
     overwrite/no-reloader→deferred.
  C. End-to-end through the REAL install handlers + a REAL Session's seams: a NEW name
     is resolvable the SAME turn; a same-name overwrite is NOT applied mid-turn but
     still lands (clobber-update preserved) at the next turn boundary.

Honesty (discovery vs resolution): these assert *resolution* (the live registry /
available-skills the op resolves against), never the LLM's mid-turn *discovery*
catalog (rebuilt once per turn) — matching the contract's explicit scope.

No mocks. Real EventLog / HotReloader / Session / RouterHostAdapter / install handlers.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.core.op_runtime.context import OpContext
from reyn.data.workspace.workspace import Workspace
from reyn.runtime.hot_reload import (
    HotReloader,
    dispatch_install_reload,
    is_pure_addition,
    set_active_hot_reloader,
)
from reyn.runtime.session import Session
from reyn.runtime.session_params import CapabilityScope
from reyn.security.permissions.permissions import PermissionDecl

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seam_recorder() -> "tuple[dict, callable]":
    """Return (ran, make_seam): ``ran`` records which seam names executed; ``make_seam``
    builds a real async seam fn that records its name and returns ``changed``."""
    ran: dict[str, int] = {}

    def make_seam(name: str, changed: bool = True):
        async def _seam(in_set: dict) -> bool:
            ran[name] = ran.get(name, 0) + 1
            return changed
        return _seam

    return ran, make_seam


def _make_session(tmp_path: Path, *, agent_name: str = "pr2-agent") -> Session:
    """Minimal real Session rooted at *tmp_path* (chdir before calling). Builds
    ``available_skills`` + ``pipeline_registry`` from ``load_config`` so entries already
    written to ``.reyn/config/*.yaml`` are adopted — mirroring session-factory."""
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    from reyn.config.loader import load_config
    from reyn.data.pipelines.registry import build_pipeline_registry
    from reyn.data.skills.registry import build_skill_registry
    cfg = load_config()
    available_skills = build_skill_registry(cfg.skills) or None
    pipeline_registry = build_pipeline_registry(cfg.pipelines, tmp_path)
    return Session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        capability_scope=CapabilityScope(available_skills=available_skills),
        pipeline_registry=pipeline_registry,
    )


def _op_ctx(tmp_path: Path, session: "Session | None") -> OpContext:
    """A real OpContext wired to *session*'s HotReloader (or None) — permission_resolver
    is None so the install handler skips the file-write gate (the gate is not under
    test here; the reload path condition is)."""
    return OpContext(
        workspace=Workspace(events=EventLog(), base_dir=tmp_path),
        events=EventLog(),
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        hot_reloader=(session._hot_reloader if session is not None else None),
    )


def _write_skill(tmp_path: Path, dirname: str, *, name: str, description: str) -> Path:
    d = tmp_path / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nbody\n", encoding="utf-8",
    )
    return d


def _write_pipeline(tmp_path: Path, filename: str, *, name: str, description: str) -> Path:
    p = tmp_path / filename
    p.write_text(
        f"pipeline: {name}\n"
        f"description: {description}\n"
        "steps:\n"
        "  - transform: {value: \"'hi'\", output: g}\n",
        encoding="utf-8",
    )
    return p


def _skill_names(session: Session) -> list[str]:
    skills = session._router_host.get_available_skills()
    return [s.name for s in skills] if skills else []


def _skill_desc(session: Session, name: str) -> "str | None":
    for s in session._router_host.get_available_skills() or []:
        if s.name == name:
            return s.description
    return None


def _pipeline_names(session: Session) -> tuple[str, ...]:
    """Names in the LIVE pipeline registry (the one run_pipeline resolves against)."""
    return session._router_host.get_pipeline_registry().names()


def _pipeline_desc(session: Session, name: str) -> "str | None":
    reg = session._router_host.get_pipeline_registry()
    return reg.get(name).description if name in reg.names() else None


# ===========================================================================
# A. HotReloader.apply_now — affected-seam-only, immediate, atomic
# ===========================================================================


@pytest.mark.asyncio
async def test_apply_now_runs_only_the_affected_seam(tmp_path: Path) -> None:
    """Tier 2: apply_now(source=skill_install) runs ONLY the "skills" seam — NOT the
    whole reload — so a mid-turn install applies just its own component."""
    ran, make_seam = _seam_recorder()
    hr = HotReloader(project_root=tmp_path, events=EventLog())
    hr.register_seam("skills", make_seam("skills"))
    hr.register_seam("pipelines", make_seam("pipelines"))
    hr.register_seam("mcp", make_seam("mcp"))

    summary = await hr.apply_now(source="skill_install")

    assert summary["applied"] == ["skills"]
    assert ran == {"skills": 1}, "pipelines/mcp seams must NOT run for a skill_install apply_now"


@pytest.mark.asyncio
async def test_apply_now_pipeline_source_targets_pipelines_seam(tmp_path: Path) -> None:
    """Tier 2: apply_now(source=pipeline_install) targets the "pipelines" seam only."""
    ran, make_seam = _seam_recorder()
    hr = HotReloader(project_root=tmp_path, events=EventLog())
    hr.register_seam("skills", make_seam("skills"))
    hr.register_seam("pipelines", make_seam("pipelines"))

    summary = await hr.apply_now(source="pipeline_install")

    assert summary["applied"] == ["pipelines"]
    assert ran == {"pipelines": 1}


@pytest.mark.asyncio
async def test_apply_now_does_not_touch_the_deferred_pending_flag(tmp_path: Path) -> None:
    """Tier 2: apply_now is the IMMEDIATE path — it never sets/clears the deferred
    ``pending`` schedule (operator /reload stays independent)."""
    _ran, make_seam = _seam_recorder()
    hr = HotReloader(project_root=tmp_path, events=EventLog())
    hr.register_seam("skills", make_seam("skills"))
    assert hr.pending is False

    await hr.apply_now(source="skill_install")

    assert hr.pending is False, "apply_now must not schedule a deferred reload"


@pytest.mark.asyncio
async def test_apply_now_unknown_source_is_noop(tmp_path: Path) -> None:
    """Tier 2: an unmapped source (e.g. operator /reload, or a not-yet-wired install
    type) applies nothing immediately — no seam runs, no crash (defensive)."""
    ran, make_seam = _seam_recorder()
    hr = HotReloader(project_root=tmp_path, events=EventLog())
    hr.register_seam("skills", make_seam("skills"))

    summary = await hr.apply_now(source="operator")

    assert summary["applied"] == []
    assert ran == {}


@pytest.mark.asyncio
async def test_apply_now_rejects_malformed_in_set_atomically(tmp_path: Path) -> None:
    """Tier 2: a malformed IN-set is REJECTED whole — the seam does NOT run (live config
    unchanged), and a config_reload_rejected event is emitted (same atomicity as
    apply_pending)."""
    cfg = tmp_path / ".reyn" / "config"
    cfg.mkdir(parents=True)
    (cfg / "skills.yaml").write_text("skills: not-a-mapping\n", encoding="utf-8")
    ran, make_seam = _seam_recorder()
    events = EventLog()
    hr = HotReloader(project_root=tmp_path, events=events)
    hr.register_seam("skills", make_seam("skills"))

    summary = await hr.apply_now(source="skill_install")

    assert "rejected" in summary
    assert ran == {}, "no seam may run when the IN-set is rejected"
    assert any(e.type == "config_reload_rejected" for e in events.all())


@pytest.mark.asyncio
async def test_apply_now_emits_config_reloaded_with_source(tmp_path: Path) -> None:
    """Tier 2: a successful immediate apply emits the config_reloaded P6 audit-event
    carrying the install source (observability of a mid-turn config change)."""
    _ran, make_seam = _seam_recorder()
    events = EventLog()
    hr = HotReloader(project_root=tmp_path, events=events)
    hr.register_seam("skills", make_seam("skills"))

    await hr.apply_now(source="skill_install")

    sources = [e.data["source"] for e in events.all() if e.type == "config_reloaded"]
    assert sources == ["skill_install"]


@pytest.mark.asyncio
async def test_operator_reload_still_runs_all_seams(tmp_path: Path) -> None:
    """Tier 2: the deferred operator path (request_reload → apply_pending) is UNCHANGED
    — it still runs ALL seams (the immediate apply_now scoping does not leak into it)."""
    ran, make_seam = _seam_recorder()
    hr = HotReloader(project_root=tmp_path, events=EventLog())
    hr.register_seam("skills", make_seam("skills"))
    hr.register_seam("pipelines", make_seam("pipelines"))
    hr.request_reload(source="operator")

    summary = await hr.apply_pending()

    assert set(summary["applied"]) == {"skills", "pipelines"}
    assert ran == {"skills": 1, "pipelines": 1}


# ===========================================================================
# B. is_pure_addition + dispatch_install_reload routing
# ===========================================================================


def test_is_pure_addition() -> None:
    """Tier 2: is_pure_addition is True only for a name absent from the current entries
    (None entries → absent → True); a present name is an overwrite (False)."""
    assert is_pure_addition("new", {"old": {}}) is True
    assert is_pure_addition("old", {"old": {}}) is False
    assert is_pure_addition("anything", None) is True
    assert is_pure_addition("x", {}) is True


@pytest.mark.asyncio
async def test_dispatch_addition_applies_immediately(tmp_path: Path) -> None:
    """Tier 2: dispatch_install_reload with is_addition=True + a live ctx reloader runs
    the affected seam IMMEDIATELY (apply_now) and does NOT schedule a deferred reload."""
    ran, make_seam = _seam_recorder()
    hr = HotReloader(project_root=tmp_path, events=EventLog())
    hr.register_seam("skills", make_seam("skills"))

    await dispatch_install_reload(hr, source="skill_install", is_addition=True)

    assert ran == {"skills": 1}
    assert hr.pending is False


@pytest.mark.asyncio
async def test_dispatch_overwrite_defers(tmp_path: Path) -> None:
    """Tier 2: dispatch_install_reload with is_addition=False keeps the EXISTING deferred
    behavior — the seam does NOT run mid-turn; a reload is scheduled for the turn
    boundary (preserving clobber-update, confining R7 to the deferred path)."""
    ran, make_seam = _seam_recorder()
    hr = HotReloader(project_root=tmp_path, events=EventLog())
    hr.register_seam("skills", make_seam("skills"))
    set_active_hot_reloader(hr)
    try:
        await dispatch_install_reload(hr, source="skill_install", is_addition=False)
    finally:
        set_active_hot_reloader(None)

    assert ran == {}, "an overwrite must not apply mid-turn"
    assert hr.pending is True, "an overwrite schedules the deferred turn-boundary reload"


@pytest.mark.asyncio
async def test_dispatch_no_reloader_defers_via_active(tmp_path: Path) -> None:
    """Tier 2: no per-session reloader (ctx.hot_reloader=None — the CLI separate-process
    install) → the deferred path via the process-active reloader, even for an addition
    (the immediate path requires a live per-session reloader)."""
    ran, make_seam = _seam_recorder()
    active = HotReloader(project_root=tmp_path, events=EventLog())
    active.register_seam("skills", make_seam("skills"))
    set_active_hot_reloader(active)
    try:
        await dispatch_install_reload(None, source="skill_install", is_addition=True)
    finally:
        set_active_hot_reloader(None)

    assert ran == {}
    assert active.pending is True


# ===========================================================================
# C. End-to-end: real install handlers + real Session seams
# ===========================================================================


@pytest.mark.asyncio
async def test_skill_install_new_name_is_live_same_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: installing a NEW skill through the real handler makes it resolvable in the
    SAME turn (present in the live get_available_skills()) WITHOUT any turn-boundary
    apply — the pure-addition immediate path. pending stays False."""
    from reyn.core.op_runtime.skill_install import handle as skill_install_handle
    from reyn.schemas.models import SkillInstallIROp

    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    assert "greet-skill" not in _skill_names(session)

    skill_dir = _write_skill(tmp_path, "greet", name="greet-skill", description="v1")
    op = SkillInstallIROp(kind="skill_install", path=str(skill_dir))
    result = await skill_install_handle(op, _op_ctx(tmp_path, session))

    assert result["status"] == "installed"
    assert "greet-skill" in _skill_names(session), (
        "a NEW skill must be resolvable the same turn (immediate mid-turn apply)"
    )
    # The immediate path fully handled it — nothing was left pending for the turn
    # boundary (apply_pending is a no-op → None when nothing is scheduled).
    residual = await session._hot_reloader.apply_pending()
    assert residual is None, (
        "the immediate addition path must not ALSO schedule a deferred reload"
    )


@pytest.mark.asyncio
async def test_skill_install_same_name_overwrite_defers_but_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: re-installing an EXISTING skill (clobber-update) is NOT applied mid-turn
    (deferred, pending scheduled) but still lands at the next turn boundary — the
    update workflow is preserved, not errored, just not same-turn (R7 avoidance)."""
    from reyn.core.op_runtime.skill_install import handle as skill_install_handle
    from reyn.schemas.models import SkillInstallIROp

    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    # First install (addition) → immediately live, description v1.
    skill_dir = _write_skill(tmp_path, "greet", name="greet-skill", description="v1")
    await skill_install_handle(
        SkillInstallIROp(kind="skill_install", path=str(skill_dir)),
        _op_ctx(tmp_path, session),
    )
    assert _skill_desc(session, "greet-skill") == "v1"

    # Re-install SAME name with a changed description (clobber-update).
    _write_skill(tmp_path, "greet", name="greet-skill", description="v2")
    result = await skill_install_handle(
        SkillInstallIROp(kind="skill_install", path=str(skill_dir)),
        _op_ctx(tmp_path, session),
    )

    assert result["status"] == "installed", "clobber-update must not be errored/refused"
    assert _skill_desc(session, "greet-skill") == "v1", (
        "a same-name overwrite must NOT be applied mid-turn (deferred)"
    )

    # It lands at the next turn boundary (the deferred turn-end apply) — proving the
    # overwrite scheduled the deferred path, and clobber-update still works.
    await session._hot_reloader.apply_pending()
    assert _skill_desc(session, "greet-skill") == "v2", (
        "the clobber-update must land at the turn boundary (applies next turn)"
    )


@pytest.mark.asyncio
async def test_pipeline_install_new_name_is_resolvable_same_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: installing a NEW pipeline through the real handler makes it resolvable in
    the SAME turn via the live registry get_pipeline_registry().get(name) — a call/match
    step could target it this execution. pending stays False."""
    from reyn.core.op_runtime.pipeline_install import handle as pipeline_install_handle
    from reyn.schemas.models import PipelineInstallIROp

    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    assert "greet.greet" not in _pipeline_names(session)

    dsl = _write_pipeline(tmp_path, "greet.yaml", name="greet", description="v1")
    op = PipelineInstallIROp(kind="pipeline_install", path=str(dsl))
    result = await pipeline_install_handle(op, _op_ctx(tmp_path, session))

    assert result["status"] == "installed"
    assert "greet.greet" in _pipeline_names(session), (
        "a NEW pipeline must be resolvable the same turn (immediate mid-turn apply)"
    )
    residual = await session._hot_reloader.apply_pending()
    assert residual is None, (
        "the immediate addition path must not ALSO schedule a deferred reload"
    )


@pytest.mark.asyncio
async def test_pipeline_install_same_name_overwrite_defers_but_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: re-installing an EXISTING pipeline (clobber-update) is NOT applied mid-turn
    (deferred) but lands at the next turn boundary — update preserved, R7 avoided."""
    from reyn.core.op_runtime.pipeline_install import handle as pipeline_install_handle
    from reyn.schemas.models import PipelineInstallIROp

    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    dsl = _write_pipeline(tmp_path, "greet.yaml", name="greet", description="v1")
    await pipeline_install_handle(
        PipelineInstallIROp(kind="pipeline_install", path=str(dsl)),
        _op_ctx(tmp_path, session),
    )
    assert _pipeline_desc(session, "greet.greet") == "v1"

    # Re-install SAME declared name with a changed description (clobber-update).
    _write_pipeline(tmp_path, "greet.yaml", name="greet", description="v2")
    result = await pipeline_install_handle(
        PipelineInstallIROp(kind="pipeline_install", path=str(dsl)),
        _op_ctx(tmp_path, session),
    )

    assert result["status"] == "installed", "clobber-update must not be errored/refused"
    assert _pipeline_desc(session, "greet.greet") == "v1", (
        "a same-name overwrite must NOT be applied mid-turn (deferred)"
    )

    await session._hot_reloader.apply_pending()
    assert _pipeline_desc(session, "greet.greet") == "v2", (
        "the clobber-update must land at the turn boundary (applies next turn)"
    )


@pytest.mark.asyncio
async def test_skill_install_no_per_session_reloader_defers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: with no per-session reloader (ctx.hot_reloader=None — the CLI
    separate-process install), a NEW skill install does NOT apply mid-turn; it takes the
    deferred path (unchanged best-effort behavior)."""
    from reyn.core.op_runtime.skill_install import handle as skill_install_handle
    from reyn.schemas.models import SkillInstallIROp

    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    set_active_hot_reloader(session._hot_reloader)
    try:
        skill_dir = _write_skill(tmp_path, "solo", name="solo-skill", description="v1")
        result = await skill_install_handle(
            SkillInstallIROp(kind="skill_install", path=str(skill_dir)),
            _op_ctx(tmp_path, None),  # ctx.hot_reloader is None
        )
    finally:
        set_active_hot_reloader(None)

    assert result["status"] == "installed"
    assert "solo-skill" not in _skill_names(session), (
        "with no per-session reloader the install must NOT apply mid-turn"
    )
    # It fell back to the deferred path — applying at the turn boundary makes it live.
    await session._hot_reloader.apply_pending()
    assert "solo-skill" in _skill_names(session), (
        "the deferred fallback lands the install at the turn boundary"
    )
