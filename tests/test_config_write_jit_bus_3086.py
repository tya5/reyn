"""Tier 2: OS invariant — #3086 skill_install / pipeline_install /
presentation_install config-write permission gate threads ``bus=`` so a
sandbox-narrowed write can be interactively approved.

Root cause (issue #3086, dogfood witness): ``require_file_write`` only fires
its JIT interactive prompt when ``bus is not None`` (mirrors
``require_http_get``'s wildcard-ask path); ``bus=None`` hard-denies outside
the effective write zone with NO prompt (docstring-declared non-interactive
behaviour). ``skill_install.handle`` / ``pipeline_install.handle`` /
``presentation_install.handle`` built the gate's ``sandbox_policy=`` kwarg
but omitted ``bus=ctx.intervention_bus`` — so even when a real
``RequestBus`` was available on ``ctx``, install writes narrowed outside the
effective zone by a phase ``SandboxPolicy`` could NEVER be interactively
approved; they always hard-denied. ``plugin_install.py``'s global-copy gate
and ``op_runtime/file.py`` already pass ``bus=ctx.intervention_bus``
correctly — this is the same fix applied to the three sibling install
handlers.

Tests (real PermissionResolver + a real-``RequestBus``-compatible Fake that
pre-answers with a scripted choice — same pattern as
tests/test_require_file_jit_ask_1505.py's ``_FakeBus`` — no mocks):

  1. skill_install: a ``SandboxPolicy`` whose ``write_paths`` excludes
     ``.reyn/config/`` narrows the config-write gate outside the effective
     zone. ``bus=None`` → PermissionError, no prompt (non-interactive
     baseline, unchanged). ``bus=`` a Fake that answers YES → the JIT prompt
     fires exactly once AND the install succeeds (config written). A Fake
     that answers "no" → PermissionError AFTER a prompt fired (denied, not
     silently blocked).
  2. Same three-way split for pipeline_install.
  3. Same three-way split for presentation_install.
  4. Strip-falsify (CLAUDE.md gate-enforcement discipline): a bare
     ``require_file_write`` call reproduces the ORIGINAL bug directly —
     omitting ``bus=`` when the resolver would otherwise ask, denies even
     though a bus that would approve is available on the context. This
     documents, in code, the exact defect class the production fix closes
     (independent of the three handler tests above happening to exercise
     the right code path).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.op_runtime.context import OpContext
from reyn.intervention_choices import NO, YES
from reyn.schemas.models import (
    PipelineInstallIROp,
    PresentationInstallIROp,
    SkillInstallIROp,
)
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.user_intervention import InterventionAnswer, UserIntervention

# ── shared stubs (real API surface, no mocks; mirrors test_plugin_install.py /
# test_require_file_jit_ask_1505.py's _FakeBus) ────────────────────────────────


class _StubWorkspace:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir


class _Events:
    subscribers: list = []

    def emit(self, *_a, **_k) -> None:
        pass


class _FakeBus:
    """Real RequestBus-compatible Fake that pre-answers with a scripted
    choice — implements the real ``request`` surface, not a mock."""

    def __init__(self, choice: str) -> None:
        self._choice = choice
        self.asks: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.asks.append(iv)
        return InterventionAnswer(text=self._choice, choice_id=self._choice)


def _make_ctx(
    tmp_path: Path, *, bus: object | None, config_filename: str,
) -> OpContext:
    """A real OpContext whose ``default_sandbox_policy.write_paths`` excludes
    ``.reyn/config/`` — narrowing the config-write gate OUTSIDE the effective
    write zone (SandboxLayer ∩ AgentLayer) even though ``.reyn/`` is the
    default-granted zone. Reproduces the exact denial shape the dogfood
    witness hit: 'outside the default write zone' despite the path living
    under ``.reyn/config/``."""
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / ".reyn" / "config").mkdir(parents=True, exist_ok=True)
    unrelated_write_dir = tmp_path / "unrelated"
    unrelated_write_dir.mkdir(parents=True, exist_ok=True)

    resolver = PermissionResolver(
        config_permissions={}, project_root=project_root, interactive=True,
    )
    decl = PermissionDecl()  # no declared grants — the zone/sandbox conjunction decides

    return OpContext(
        workspace=_StubWorkspace(base_dir=project_root),
        events=_Events(),
        permission_decl=decl,
        permission_resolver=resolver,
        actor="test",
        intervention_bus=bus,
        subscribers=[],
        default_sandbox_policy={"write_paths": [str(unrelated_write_dir)]},
    )


def _make_skill_dir(base: Path, name: str = "my-skill") -> Path:
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: a test skill\n---\n\nSkill body.\n",
        encoding="utf-8",
    )
    return skill_dir


def _make_pipeline_dsl(base: Path, name: str = "hello") -> Path:
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{name}.yaml"
    path.write_text(
        f"pipeline: {name}\n"
        "description: A test pipeline\n"
        "steps:\n"
        "  - transform: {value: \"1 + 1\", output: two}\n",
        encoding="utf-8",
    )
    return path


_VALID_BLUEPRINT = {"component": "text", "text": "hello"}


# ── 1. skill_install ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skill_install_config_write_bus_none_denies(tmp_path):
    """Tier 2: #3086 — non-interactive baseline unchanged: bus=None + the
    config-write gate narrowed outside the effective zone → PermissionError,
    no config written."""
    from reyn.core.op_runtime.skill_install import handle

    skill_dir = _make_skill_dir(tmp_path / "src")
    ctx = _make_ctx(tmp_path, bus=None, config_filename="skills.yaml")
    op = SkillInstallIROp(kind="skill_install", path=str(skill_dir))

    with pytest.raises(PermissionError):
        await handle(op, ctx)

    config_path = ctx.workspace.base_dir / ".reyn" / "config" / "skills.yaml"
    assert not config_path.exists() or "my-skill" not in config_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_skill_install_config_write_bus_approves(tmp_path):
    """Tier 2: #3086 fix — a real bus threaded through the gate lets the
    operator interactively approve the narrowed write; install succeeds and
    the JIT prompt fired exactly once. RED if bus= is not passed through to
    require_file_write (the #3086 regression)."""
    from reyn.core.op_runtime.skill_install import handle

    skill_dir = _make_skill_dir(tmp_path / "src")
    bus = _FakeBus(YES)
    ctx = _make_ctx(tmp_path, bus=bus, config_filename="skills.yaml")
    op = SkillInstallIROp(kind="skill_install", path=str(skill_dir))

    result = await handle(op, ctx)

    assert result["status"] == "installed", f"install failed: {result}"
    (ask,) = bus.asks  # exactly one prompt fired (tuple-unpack: RED if fired 0 or 2+ times)
    assert "skills.yaml" in ask.prompt or "skills.yaml" in ask.detail
    config_path = ctx.workspace.base_dir / ".reyn" / "config" / "skills.yaml"
    assert "my-skill" in config_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_skill_install_config_write_bus_denies_after_prompt(tmp_path):
    """Tier 2: #3086 — the operator can also DENY via the prompt (not just
    the historical hard-deny); PermissionError still raised, but only after
    a prompt fired."""
    from reyn.core.op_runtime.skill_install import handle

    skill_dir = _make_skill_dir(tmp_path / "src")
    bus = _FakeBus(NO)
    ctx = _make_ctx(tmp_path, bus=bus, config_filename="skills.yaml")
    op = SkillInstallIROp(kind="skill_install", path=str(skill_dir))

    with pytest.raises(PermissionError):
        await handle(op, ctx)

    (ask,) = bus.asks  # user was asked exactly once before the denial
    assert "skills.yaml" in ask.prompt or "skills.yaml" in ask.detail


# ── 2. pipeline_install ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_install_config_write_bus_none_denies(tmp_path):
    """Tier 2: #3086 — non-interactive baseline unchanged for pipeline_install."""
    from reyn.core.op_runtime.pipeline_install import handle

    dsl_path = _make_pipeline_dsl(tmp_path / "src")
    ctx = _make_ctx(tmp_path, bus=None, config_filename="pipelines.yaml")
    op = PipelineInstallIROp(kind="pipeline_install", path=str(dsl_path))

    with pytest.raises(PermissionError):
        await handle(op, ctx)


@pytest.mark.asyncio
async def test_pipeline_install_config_write_bus_approves(tmp_path):
    """Tier 2: #3086 fix — pipeline_install threads bus= through the
    pipelines.yaml gate; a narrowed write is interactively approvable."""
    from reyn.core.op_runtime.pipeline_install import handle

    dsl_path = _make_pipeline_dsl(tmp_path / "src")
    bus = _FakeBus(YES)
    ctx = _make_ctx(tmp_path, bus=bus, config_filename="pipelines.yaml")
    op = PipelineInstallIROp(kind="pipeline_install", path=str(dsl_path))

    result = await handle(op, ctx)

    assert result["status"] == "installed", f"install failed: {result}"
    (ask,) = bus.asks  # exactly one prompt fired
    assert "pipelines.yaml" in ask.prompt or "pipelines.yaml" in ask.detail
    config_path = ctx.workspace.base_dir / ".reyn" / "config" / "pipelines.yaml"
    assert "hello" in config_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_pipeline_install_config_write_bus_denies_after_prompt(tmp_path):
    """Tier 2: #3086 — pipeline_install operator denial via prompt."""
    from reyn.core.op_runtime.pipeline_install import handle

    dsl_path = _make_pipeline_dsl(tmp_path / "src")
    bus = _FakeBus(NO)
    ctx = _make_ctx(tmp_path, bus=bus, config_filename="pipelines.yaml")
    op = PipelineInstallIROp(kind="pipeline_install", path=str(dsl_path))

    with pytest.raises(PermissionError):
        await handle(op, ctx)

    (ask,) = bus.asks  # user was asked exactly once before the denial
    assert "pipelines.yaml" in ask.prompt or "pipelines.yaml" in ask.detail


# ── 3. presentation_install ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_presentation_install_config_write_bus_none_denies(tmp_path):
    """Tier 2: #3086 sibling — presentation_install has the SAME config-write
    gate shape as skill/pipeline install (same fix-class); non-interactive
    baseline unchanged."""
    from reyn.core.op_runtime.presentation_install import handle

    ctx = _make_ctx(tmp_path, bus=None, config_filename="presentations.yaml")
    op = PresentationInstallIROp(
        kind="presentation_install", name="card_a", blueprint=_VALID_BLUEPRINT,
    )

    with pytest.raises(PermissionError):
        await handle(op, ctx)


@pytest.mark.asyncio
async def test_presentation_install_config_write_bus_approves(tmp_path):
    """Tier 2: #3086 sibling fix — presentation_install threads bus= through
    the presentations.yaml gate."""
    from reyn.core.op_runtime.presentation_install import handle

    bus = _FakeBus(YES)
    ctx = _make_ctx(tmp_path, bus=bus, config_filename="presentations.yaml")
    op = PresentationInstallIROp(
        kind="presentation_install", name="card_a", blueprint=_VALID_BLUEPRINT,
    )

    result = await handle(op, ctx)

    assert result["status"] == "installed", f"install failed: {result}"
    (ask,) = bus.asks  # exactly one prompt fired
    assert "presentations.yaml" in ask.prompt or "presentations.yaml" in ask.detail
    config_path = ctx.workspace.base_dir / ".reyn" / "config" / "presentations.yaml"
    assert "card_a" in config_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_presentation_install_config_write_bus_denies_after_prompt(tmp_path):
    """Tier 2: #3086 sibling — presentation_install operator denial via prompt."""
    from reyn.core.op_runtime.presentation_install import handle

    bus = _FakeBus(NO)
    ctx = _make_ctx(tmp_path, bus=bus, config_filename="presentations.yaml")
    op = PresentationInstallIROp(
        kind="presentation_install", name="card_a", blueprint=_VALID_BLUEPRINT,
    )

    with pytest.raises(PermissionError):
        await handle(op, ctx)

    (ask,) = bus.asks  # user was asked exactly once before the denial
    assert "presentations.yaml" in ask.prompt or "presentations.yaml" in ask.detail


# ── 4. strip-falsify — the defect class in isolation ──────────────────────────


@pytest.mark.asyncio
async def test_strip_falsify_omitting_bus_reproduces_original_bug(tmp_path):
    """Tier 2: #3086 — strip-falsify at the PermissionResolver level, proving
    the defect class the production fix closes: a config-write gate call that
    OMITS ``bus=`` (the exact pre-fix shape of skill_install.py /
    pipeline_install.py / presentation_install.py's require_file_write call)
    denies even when a bus that WOULD approve is sitting right there on the
    context. Passing that same bus through (the fix) succeeds. RED if
    require_file_write's bus=None default ever stops mattering (i.e. if
    outside-zone writes start being silently granted regardless of bus)."""
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)
    unrelated_write_dir = tmp_path / "unrelated"
    unrelated_write_dir.mkdir(parents=True, exist_ok=True)
    config_path = project_root / ".reyn" / "config" / "skills.yaml"

    resolver = PermissionResolver(
        config_permissions={}, project_root=project_root, interactive=True,
    )
    decl = PermissionDecl()
    from reyn.security.sandbox.policy import SandboxPolicy

    sandbox = SandboxPolicy(write_paths=[str(unrelated_write_dir)])
    bus_that_would_approve = _FakeBus(YES)

    # Pre-fix shape: bus= omitted (mirrors the #3086 bug verbatim).
    with pytest.raises(PermissionError):
        await resolver.require_file_write(
            decl, str(config_path), "test", sandbox_policy=sandbox,
        )
    assert not bus_that_would_approve.asks, "a bus that was never passed cannot have been consulted"

    # Fixed shape: bus= threaded through — same resolver, same sandbox, same path.
    await resolver.require_file_write(
        decl, str(config_path), "test", sandbox_policy=sandbox, bus=bus_that_would_approve,
    )
    (ask,) = bus_that_would_approve.asks  # exactly one prompt fired
    assert "skills.yaml" in ask.prompt or "skills.yaml" in ask.detail
