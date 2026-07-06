"""Tier 2: OS invariant — pipeline_install op (local + source/git install, mirrors
skill_install's PR-C/PR-D coverage).

Tests:
  1. e2e local install: a real pipeline DSL file → handler writes pipelines.yaml
     entry, build_pipeline_registry picks it up.
  2. truncate-falsify (CLAUDE.md mandatory recovery gate): install a pipeline →
     truncate WAL below the generation's source seq → reconstruct as-of-cut →
     installed pipeline SURVIVES.
  3. threat-scan block: a DSL description that triggers a blocking threat →
     handler returns status="blocked", no config write.
  4. trust floor: pipeline_management__install_local / __install_source are denied
     under the builtin_untrusted_profile (mirrors the skill-install floor).
  5. name-mismatch refusal: op.name disagreeing with the DSL's declared
     'pipeline:' name is refused (fail-loud, the pipeline-specific validation
     rule distinct from skill's freely-renaming op.name).
  6. e2e source install via a local git remote (file:// URL); threat-scan block
     + clone removal; require_http_get gate; subdir convention; config-generation
     smoke; sandbox-routed clone.
  7. catalog reachability (the #2589/#2621 bug class): pipeline_management__*
     verbs actually appear in list_actions(category=["pipeline_management"]),
     not just dispatchable via invoke_action.

Real PermissionResolver + StateLog + OpContext + AgentRegistry throughout (no mocks).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from reyn.core.events.config_recovery import record_config_generation
from reyn.core.events.snapshot_generations import rewind as _wal_rewind
from reyn.core.events.state_log import StateLog
from reyn.core.op_runtime.context import OpContext
from reyn.data.pipelines.registry import build_pipeline_registry
from reyn.runtime.registry import AgentRegistry
from reyn.schemas.models import PipelineInstallIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver

# ── shared stubs (mirrors test_skill_install_pr_c.py / pr_d.py) ──────────────


class _StubWorkspace:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir


class _Events:
    """Minimal real-callable event log stub — records emitted events for assertion."""

    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    def emit(self, kind: str, **kwargs) -> None:
        self.emitted.append((kind, kwargs))


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _make_ctx(
    tmp_path: Path,
    *,
    state_log: StateLog | None = None,
    http_get_approved: bool = True,
) -> tuple[OpContext, _Events]:
    """Build a real OpContext with a PermissionResolver that approves
    pipelines.yaml writes and (optionally) http.get for the source host."""
    config_path = tmp_path / ".reyn" / "config" / "pipelines.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=False,
    )
    resolver.session_approve_path(str(config_path), "test", "file.write")
    if http_get_approved:
        resolver.session_approve_host("*", "test")

    decl = PermissionDecl(
        file_write=[{"path": str(config_path), "scope": "just_path"}],
        http_get=[{"host": "*"}],
    )
    events = _Events()
    ctx = OpContext(
        workspace=_StubWorkspace(base_dir=tmp_path),
        events=events,
        permission_decl=decl,
        permission_resolver=resolver,
        actor="test",
        intervention_bus=None,
        subscribers=[],
        state_log=state_log,
    )
    return ctx, events


def _make_pipeline_dsl(
    base: Path, filename: str = "hello.yaml", name: str = "hello",
    description: str = "A test pipeline",
) -> Path:
    """Write a minimal valid pipeline DSL file."""
    base.mkdir(parents=True, exist_ok=True)
    path = base / filename
    path.write_text(
        f"pipeline: {name}\n"
        f"description: {description}\n"
        "steps:\n"
        "  - transform: {value: \"1 + 1\", output: two}\n",
        encoding="utf-8",
    )
    return path


def _make_git_pipeline_repo(
    base: Path, filename: str = "hello.yaml", name: str = "source-pipeline",
    description: str = "From git",
) -> Path:
    """Create a minimal git repo containing a pipeline DSL file at the root."""
    repo = base / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _make_pipeline_dsl(repo, filename, name, description)
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)
    return repo


def _make_git_pipeline_repo_with_subdir(
    base: Path, subdir: str = "pipelines", filename: str = "hello.yaml",
    name: str = "subdir-pipeline", description: str = "In a subdir",
) -> Path:
    repo = base / "monorepo"
    (repo / subdir).mkdir(parents=True, exist_ok=True)
    _make_pipeline_dsl(repo / subdir, filename, name, description)
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)
    return repo


# ── Test 1: e2e local install ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_install_e2e_writes_config_and_registry_picks_up(tmp_path):
    """Tier 2: a real pipeline_install op writes the pipelines.yaml entry and
    build_pipeline_registry returns the installed pipeline. RED if the config
    write is missing or build_pipeline_registry does not load it."""
    from reyn.core.op_runtime.pipeline_install import handle

    dsl_path = _make_pipeline_dsl(tmp_path / "pipelines", "hello.yaml", "hello", "Does something useful")
    ctx, _events = _make_ctx(tmp_path)

    op = PipelineInstallIROp(kind="pipeline_install", path=str(dsl_path))
    result = await handle(op=op, ctx=ctx)

    assert result["status"] == "installed", f"install failed: {result}"
    assert result["name"] == "hello"
    assert result["description"] == "Does something useful"

    config_path = tmp_path / ".reyn" / "config" / "pipelines.yaml"
    assert config_path.exists(), "pipelines.yaml was not written"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    entry = raw["pipelines"]["entries"]["hello"]
    assert entry["enabled"] is True
    assert "hello.yaml" in entry["path"]
    assert entry["description"] == "Does something useful"

    registry = build_pipeline_registry(raw["pipelines"], tmp_path)
    assert "hello" in registry.names()
    assert registry.get("hello").description == "Does something useful"


# ── Test 2: truncate-falsify (MANDATORY CLAUDE.md recovery gate) ─────────────


@pytest.mark.asyncio
async def test_pipeline_install_truncate_falsify_generation_survives_wal_truncation(tmp_path):
    """Tier 2: MANDATORY recovery gate (CLAUDE.md) — a REAL pipeline_install op
    (state_log threaded via OpContext) records a config generation; WAL
    truncation below the source seq does NOT lose the installed pipeline (the
    generation stores full-state, not WAL events).

    RED if record_config_generation was not called by the handler (config
    invisible to recovery) or if _reconcile_config_as_of_cut trusts the
    on-disk yaml instead of the generation truth."""
    from reyn.core.op_runtime.pipeline_install import handle

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    pipelines_path = tmp_path / ".reyn" / "config" / "pipelines.yaml"
    pipelines_path.parent.mkdir(parents=True, exist_ok=True)

    empty_config: dict = {}
    pipelines_path.write_text(
        yaml.dump(empty_config) if empty_config else "", encoding="utf-8",
    )
    await record_config_generation(state_log, str(pipelines_path), empty_config)
    cut = state_log.current_seq

    await state_log.append("inbox_put", n=0)

    dsl_path = _make_pipeline_dsl(tmp_path / "pipelines", "recover.yaml", "recover-pipeline", "Recoverable pipeline")
    ctx, _events = _make_ctx(tmp_path, state_log=state_log)
    op = PipelineInstallIROp(kind="pipeline_install", path=str(dsl_path))
    result = await handle(op=op, ctx=ctx)
    assert result["status"] == "installed", f"install failed: {result}"

    raw_after = yaml.safe_load(pipelines_path.read_text(encoding="utf-8")) or {}
    assert "recover-pipeline" in raw_after.get("pipelines", {}).get("entries", {}), \
        "pipeline not in config after install"

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )
    reg._reconcile_config_as_of_cut(state_log.current_seq)
    post_install = yaml.safe_load(pipelines_path.read_text(encoding="utf-8")) or {}
    assert "recover-pipeline" in post_install.get("pipelines", {}).get("entries", {}), \
        "reconcile-as-of-now lost the pipeline"

    await _wal_rewind(state_log, target_n=cut)
    reg._reconcile_config_as_of_cut(cut)
    reverted = yaml.safe_load(pipelines_path.read_text(encoding="utf-8")) or {}
    assert "recover-pipeline" not in reverted.get("pipelines", {}).get("entries", {}), \
        "rewind did not revert the installed pipeline — generation not recorded correctly"


# ── Test 3: threat-scan block ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_install_threat_scan_blocks_on_matching_description(tmp_path, monkeypatch):
    """Tier 2: when threat-scan is enabled and the DSL description matches a
    blocking threat pattern, the handler returns status='blocked' and does NOT
    write pipelines.yaml."""
    from reyn.core.op_runtime.pipeline_install import handle

    dsl_path = _make_pipeline_dsl(
        tmp_path / "pipelines", "evil.yaml", "evil-pipeline", "EVIL_THREAT_MARKER",
    )

    class _ThreatMatch:
        def __init__(self):
            self.pattern_id = "test-threat"
            self.severity = "block"
            self.scope = "strict"

    class _FakeThreatScanConfig:
        enabled = True
        block_severity = "block"

    ctx, _events = _make_ctx(tmp_path)
    ctx.threat_scan = _FakeThreatScanConfig()  # type: ignore[attr-defined]

    def _fake_scan(content, config, *, scope="context"):
        if "EVIL_THREAT_MARKER" in content:
            return [_ThreatMatch()]
        return []

    monkeypatch.setattr("reyn.core.op_runtime.pipeline_install.scan_for_threats", _fake_scan)
    monkeypatch.setattr(
        "reyn.core.op_runtime.pipeline_install.first_blocking_match",
        lambda matches, threshold="block": matches[0] if matches else None,
    )

    op = PipelineInstallIROp(kind="pipeline_install", path=str(dsl_path))
    result = await handle(op=op, ctx=ctx)

    assert result["status"] == "blocked", f"expected blocked, got {result}"
    config_path = tmp_path / ".reyn" / "config" / "pipelines.yaml"
    assert not config_path.exists() or (
        yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    ).get("pipelines", {}).get("entries", {}).get("evil-pipeline") is None, \
        "pipelines.yaml was written despite a blocking threat match"


# ── Test 4: trust floor ───────────────────────────────────────────────────────


def test_pipeline_install_local_is_denied_under_untrusted_floor() -> None:
    """Tier 2: pipeline_management__install_local is in the builtin_untrusted_profile
    deny set (mirrors the skill-install floor)."""
    from reyn.security.permissions.capability_profile import (
        _BUILTIN_UNTRUSTED_DENY,
        _FLOORED_QUALIFIED,
        builtin_untrusted_profile,
        resolve_profile,
    )
    from reyn.security.permissions.effective import tool_contextually_denied
    from reyn.tools.universal_dispatch import unwrapped_tool_name

    assert "pipeline-install" in _FLOORED_QUALIFIED, "pipeline-install class missing from _FLOORED_QUALIFIED"
    assert "pipeline_management__install_local" in _FLOORED_QUALIFIED["pipeline-install"], \
        "pipeline_management__install_local not in the pipeline-install floor class"

    assert "pipeline_management__install_local" in _BUILTIN_UNTRUSTED_DENY, \
        "pipeline_management__install_local not in _BUILTIN_UNTRUSTED_DENY"

    bare = unwrapped_tool_name("pipeline_management__install_local")
    assert bare is not None, \
        "pipeline_management__install_local has no _OPERATION_RULES entry — bare alias cannot be derived"
    assert bare in _BUILTIN_UNTRUSTED_DENY, f"bare alias {bare!r} not in _BUILTIN_UNTRUSTED_DENY"

    contextual, _ = resolve_profile(builtin_untrusted_profile())
    assert tool_contextually_denied(contextual, "pipeline_management__install_local"), \
        "untrusted floor does not deny pipeline_management__install_local at the live gate"
    assert tool_contextually_denied(contextual, bare), \
        f"untrusted floor does not deny bare alias {bare!r} at the live gate"


def test_pipeline_install_source_is_denied_under_untrusted_floor() -> None:
    """Tier 2: pipeline_management__install_source is in the builtin_untrusted_profile
    deny set (source install — higher risk than local, adds HTTP trust boundary)."""
    from reyn.security.permissions.capability_profile import (
        _BUILTIN_UNTRUSTED_DENY,
        _FLOORED_QUALIFIED,
        builtin_untrusted_profile,
        resolve_profile,
    )
    from reyn.security.permissions.effective import tool_contextually_denied
    from reyn.tools.universal_dispatch import unwrapped_tool_name

    assert "pipeline_management__install_source" in _FLOORED_QUALIFIED["pipeline-install"]
    assert "pipeline_management__install_source" in _BUILTIN_UNTRUSTED_DENY

    bare = unwrapped_tool_name("pipeline_management__install_source")
    assert bare is not None
    assert bare in _BUILTIN_UNTRUSTED_DENY

    contextual, _ = resolve_profile(builtin_untrusted_profile())
    assert tool_contextually_denied(contextual, "pipeline_management__install_source")
    assert tool_contextually_denied(contextual, bare)


# ── Test 5: name-mismatch refusal (pipeline-specific validation rule) ─────────


@pytest.mark.asyncio
async def test_pipeline_install_op_name_mismatch_with_declared_name_refused(tmp_path):
    """Tier 2: unlike skill_install (op.name freely renames the registered key),
    a pipeline's declared 'pipeline:' name is ALWAYS the resolution key a
    call/match step targets — op.name disagreeing with it is refused rather
    than silently diverging the config key from the resolution key."""
    from reyn.core.op_runtime.pipeline_install import handle

    dsl_path = _make_pipeline_dsl(tmp_path / "pipelines", "hello.yaml", "hello", "desc")
    ctx, _events = _make_ctx(tmp_path)

    op = PipelineInstallIROp(kind="pipeline_install", path=str(dsl_path), name="not-hello")
    result = await handle(op=op, ctx=ctx)

    assert result["status"] == "error", f"expected error, got {result}"
    assert "mismatch" in result["error"].lower()

    config_path = tmp_path / ".reyn" / "config" / "pipelines.yaml"
    assert not config_path.exists() or not (
        yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    ).get("pipelines", {}).get("entries", {}), \
        "pipelines.yaml must not gain an entry when the name mismatch is refused"


# ── Test 6: source install (mirrors PR-D) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_install_source_e2e_clones_and_registers(tmp_path):
    """Tier 2: a real pipeline_install op with source= clones the git repo to
    .reyn/pipelines/<name>/, writes pipelines.yaml, and the entry path points
    to the installed clone."""
    from reyn.core.op_runtime.pipeline_install import handle

    repo = _make_git_pipeline_repo(tmp_path / "repos", "hello.yaml", "source-pipeline", "A git-sourced pipeline")
    source_url = repo.as_uri()

    ctx, events = _make_ctx(tmp_path)
    op = PipelineInstallIROp(kind="pipeline_install", source=source_url)
    result = await handle(op=op, ctx=ctx)

    assert result["status"] == "installed", f"expected installed, got {result}"
    assert result["name"] == "source-pipeline"
    assert result["description"] == "A git-sourced pipeline"
    assert result["source"] == source_url

    install_path = Path(result["path"])
    assert ".reyn" in install_path.parts, f"installed path not under .reyn/: {install_path}"
    assert install_path.exists(), f"installed DSL file not found at {install_path}"

    config_path = tmp_path / ".reyn" / "config" / "pipelines.yaml"
    assert config_path.exists(), "pipelines.yaml was not written"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    entry = raw["pipelines"]["entries"]["source-pipeline"]
    assert entry["enabled"] is True
    assert entry["source"] == source_url
    assert ".reyn" in entry["path"]

    kinds = [k for k, _ in events.emitted]
    assert "pipeline_installed" in kinds, f"pipeline_installed event not emitted; got {kinds}"


@pytest.mark.asyncio
async def test_pipeline_install_source_threat_scan_blocks_and_removes_clone(tmp_path, monkeypatch):
    """Tier 2: when the fetched DSL description triggers a blocking threat, the
    handler returns status='blocked', the clone is removed, and pipelines.yaml
    is NOT written."""
    from reyn.core.op_runtime.pipeline_install import handle

    repo = _make_git_pipeline_repo(tmp_path / "repos", "hello.yaml", "evil-pipeline", "EVIL_THREAT_MARKER")
    source_url = repo.as_uri()

    class _ThreatMatch:
        def __init__(self):
            self.pattern_id = "test-threat"
            self.severity = "block"
            self.scope = "strict"

    class _FakeThreatScanConfig:
        enabled = True
        block_severity = "block"

    ctx, _events = _make_ctx(tmp_path)
    ctx.threat_scan = _FakeThreatScanConfig()  # type: ignore[attr-defined]

    def _fake_scan(content, config, *, scope="context"):
        if "EVIL_THREAT_MARKER" in content:
            return [_ThreatMatch()]
        return []

    monkeypatch.setattr("reyn.core.op_runtime.pipeline_install.scan_for_threats", _fake_scan)
    monkeypatch.setattr(
        "reyn.core.op_runtime.pipeline_install.first_blocking_match",
        lambda matches, threshold="block": matches[0] if matches else None,
    )

    op = PipelineInstallIROp(kind="pipeline_install", source=source_url)
    result = await handle(op=op, ctx=ctx)

    assert result["status"] == "blocked", f"expected blocked, got {result}"

    config_path = tmp_path / ".reyn" / "config" / "pipelines.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    assert (raw or {}).get("pipelines", {}).get("entries", {}).get("evil-pipeline") is None, \
        "pipelines.yaml was written despite a blocking threat match"

    clone_dir = tmp_path / ".reyn" / "pipelines" / "evil-pipeline"
    assert not clone_dir.exists(), f"clone directory {clone_dir} was not removed after threat block"


@pytest.mark.asyncio
async def test_pipeline_install_source_gates_http_get(tmp_path, monkeypatch):
    """Tier 2: the handler calls require_http_get for non-local (https/ssh)
    sources. When http.get is NOT approved, a PermissionError is raised before
    any clone."""
    from reyn.core.op_runtime import pipeline_install as _pi_mod
    from reyn.core.op_runtime.pipeline_install import handle

    async def _fake_clone(git_url, dest, ctx):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "hello.yaml").write_text(
            "pipeline: gated-pipeline\ndescription: Gated\nsteps:\n"
            "  - transform: {value: \"1\", output: o}\n",
            encoding="utf-8",
        )
        return None

    monkeypatch.setattr(_pi_mod, "_shallow_clone", _fake_clone)

    source_url = "https://github.com/example/gated-pipeline"
    ctx, _events = _make_ctx(tmp_path, http_get_approved=False)

    op = PipelineInstallIROp(kind="pipeline_install", source=source_url)
    with pytest.raises(PermissionError):
        await handle(op=op, ctx=ctx)

    clone_dir = tmp_path / ".reyn" / "pipelines" / "gated-pipeline"
    assert not clone_dir.exists(), "clone was created despite missing http.get permission"


@pytest.mark.asyncio
async def test_pipeline_install_source_subdir_convention(tmp_path):
    """Tier 2: a source URL with '//' subdir separator installs the pipeline
    from the specified subdirectory, selecting the DSL file via 'path'."""
    from reyn.core.op_runtime.pipeline_install import handle

    repo = _make_git_pipeline_repo_with_subdir(
        tmp_path / "repos", subdir="pipelines", filename="hello.yaml",
        name="subdir-pipeline", description="Lives in a subdir",
    )
    source_url = repo.as_uri() + "//pipelines"

    ctx, _events = _make_ctx(tmp_path)
    op = PipelineInstallIROp(kind="pipeline_install", source=source_url)
    result = await handle(op=op, ctx=ctx)

    assert result["status"] == "installed", f"expected installed, got {result}"
    assert result["name"] == "subdir-pipeline"
    assert result["description"] == "Lives in a subdir"


@pytest.mark.asyncio
async def test_pipeline_install_source_records_config_generation(tmp_path):
    """Tier 2: a source install calls record_config_generation — the recovery
    contract applies to source installs just as to local ones."""
    from reyn.core.events.config_generations import ConfigGenerationStore  # noqa: F401
    from reyn.core.events.config_recovery import config_generations_dir
    from reyn.core.op_runtime.pipeline_install import handle

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    repo = _make_git_pipeline_repo(tmp_path / "repos", "hello.yaml", "recover-source-pipeline", "Recoverable source pipeline")
    source_url = repo.as_uri()

    ctx, _events = _make_ctx(tmp_path, state_log=state_log)
    op = PipelineInstallIROp(kind="pipeline_install", source=source_url)
    result = await handle(op=op, ctx=ctx)

    assert result["status"] == "installed", f"install failed: {result}"

    gen_dir = config_generations_dir(tmp_path / ".reyn")
    assert gen_dir.exists(), "config generations dir was not created — record_config_generation not called"
    assert list(gen_dir.glob("*.yaml")), f"no generation files in {gen_dir}"


@pytest.mark.asyncio
async def test_pipeline_install_clone_routes_through_sandbox_abstraction(tmp_path):
    """Tier 2: SECURITY — the git clone subprocess must go THROUGH the sandbox
    abstraction (reused verbatim from skill_install's _shallow_clone) — verified
    via a REAL NoopBackend subclass that records each run() invocation."""
    from reyn.core.op_runtime.pipeline_install import handle
    from reyn.security.sandbox.noop_backend import NoopBackend

    class _RecordingBackend(NoopBackend):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[list[str]] = []

        async def run(self, argv, policy, **kwargs):
            self.calls.append(list(argv))
            return await super().run(argv, policy, **kwargs)

    repo = _make_git_pipeline_repo(tmp_path / "repos", "hello.yaml", "routed-pipeline", "Routed through sandbox")
    source_url = repo.as_uri()

    ctx, _events = _make_ctx(tmp_path)
    recording_backend = _RecordingBackend()
    ctx.sandbox_backend = recording_backend

    op = PipelineInstallIROp(kind="pipeline_install", source=source_url)
    result = await handle(op=op, ctx=ctx)

    assert result["status"] == "installed", f"expected installed, got {result}"
    assert recording_backend.calls, "git clone did not route through the injected sandbox backend's run()"
    clone_call = recording_backend.calls[0]
    assert clone_call[:2] == ["git", "clone"], f"unexpected clone argv: {clone_call}"
    assert source_url in clone_call


# ── Test 7: catalog reachability (the #2589/#2621 bug class) ─────────────────


@pytest.mark.asyncio
async def test_pipeline_management_verbs_appear_in_list_actions() -> None:
    """Tier 2c: pipeline_management__install_local / __install_source must
    actually appear in list_actions(category=["pipeline_management"]) — NOT
    just be dispatchable via invoke_action. RED against a catalog wiring that
    registers + dispatches a verb but never enumerates it (the exact
    #2589/#2621 'registered + dispatchable but LLM-invisible' bug class this
    PR was warned to avoid — and which was found to ALSO affect the
    pre-existing skill_management category while wiring this)."""
    from reyn.tools.types import ToolContext
    from reyn.tools.universal_catalog import LIST_ACTIONS

    ctx = ToolContext(
        events=_Events(), permission_resolver=None, workspace=None,
        caller_kind="router", router_state=None,
    )

    result = await LIST_ACTIONS.handler({"category": ["pipeline_management"]}, ctx)

    names = {it["qualified_name"] for it in result["items"]}
    assert "pipeline_management__install_local" in names, \
        f"pipeline_management__install_local not enumerated; got {names}"
    assert "pipeline_management__install_source" in names, \
        f"pipeline_management__install_source not enumerated; got {names}"


@pytest.mark.asyncio
async def test_skill_management_verbs_also_reachable_via_list_actions() -> None:
    """Tier 2c: companion assertion — skill_management__install_local /
    __install_source (the pre-existing category found to have the SAME
    enumeration gap while wiring pipeline_management) are now ALSO enumerable.
    RED if the skill_management fix (adding it alongside pipeline_management
    to the static-enumeration list) regresses."""
    from reyn.tools.types import ToolContext
    from reyn.tools.universal_catalog import LIST_ACTIONS

    ctx = ToolContext(
        events=_Events(), permission_resolver=None, workspace=None,
        caller_kind="router", router_state=None,
    )

    result = await LIST_ACTIONS.handler({"category": ["skill_management"]}, ctx)

    names = {it["qualified_name"] for it in result["items"]}
    assert "skill_management__install_local" in names
    assert "skill_management__install_source" in names
