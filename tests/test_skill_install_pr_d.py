"""Tier 2: OS invariant — #2548 PR-D skill_install source/git install.

Tests:
  1. e2e source install: a local git repo (file:// URL) as the source → handler
     clones it, writes skills.yaml entry, entry path points under .reyn/skills/.
  2. Threat-scan block on a fetched SKILL.md with a malicious description →
     status="blocked", clone removed, no config write.
  3. require_http_get gate is exercised on the source host.
  4. Subdir convention: "file:///path/to/repo//subdir" selects a subdirectory inside
     the cloned repo.
  5. config-generation smoke: source install records a config generation (handler
     calls record_config_generation — same recovery contract as install_local).
  6/7. SECURITY: path-traversal → arbitrary rmtree is refused for both the
     frontmatter ``name:`` (third-party content) and caller ``op.name`` — a sentinel
     dir outside .reyn/skills/ survives the attempted install.
  8. _safe_skill_name unit: rejects traversal / separators / hidden names.

Real PermissionResolver + StateLog + OpContext throughout (no mocks for
collaborators; monkeypatch used only for the threat-scan scan_for_threats callable
in the threat-block test, which is the documented testing idiom from PR-C).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.core.op_runtime.context import OpContext
from reyn.runtime.registry import AgentRegistry
from reyn.schemas.models import SkillInstallIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver

# ── shared stubs (mirrors test_skill_install_pr_c.py) ─────────────────────────


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
    """Build a real OpContext with a PermissionResolver that approves skills.yaml writes
    and (optionally) http.get for the source host."""
    config_path = tmp_path / ".reyn" / "config" / "skills.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=False,
    )
    resolver.session_approve_path(str(config_path), "test", "file.write")
    if http_get_approved:
        # Pre-approve http.get for all hosts (= wildcard approval for test isolation).
        # The live gate still runs the resolver path; this bypasses the interactive
        # prompt so tests don't block on stdin.
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


def _make_git_skill_repo(base: Path, name: str = "source-skill", description: str = "From git") -> Path:
    """Create a minimal git repo containing a SKILL.md at the root."""
    repo = base / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    skill_md = repo / "SKILL.md"
    skill_md.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nSkill body.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)
    return repo


def _make_git_skill_repo_with_subdir(
    base: Path,
    subdir: str = "skills/my-skill",
    name: str = "subdir-skill",
    description: str = "In a subdir",
) -> Path:
    """Create a git repo with the SKILL.md under a subdirectory."""
    repo = base / "monorepo"
    skill_dir = repo / subdir
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nSkill body.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)
    return repo


# ── Test 1: e2e source install via local file:// git remote ──────────────────


@pytest.mark.asyncio
async def test_skill_install_source_e2e_clones_and_registers(tmp_path):
    """Tier 2: a real skill_install op with source= clones the git repo to
    .reyn/skills/<name>/, writes skills.yaml, and the entry path points to the
    installed clone. RED if the clone is absent or skills.yaml lacks the entry."""
    from reyn.core.op_runtime.skill_install import handle

    repo = _make_git_skill_repo(tmp_path / "repos", "source-skill", "A git-sourced skill")
    source_url = repo.as_uri()  # file:///...

    ctx, events = _make_ctx(tmp_path)
    op = SkillInstallIROp(kind="skill_install", source=source_url)
    result = await handle(op=op, ctx=ctx)

    assert result["status"] == "installed", f"expected installed, got {result}"
    assert result["name"] == "source-skill"
    assert result["description"] == "A git-sourced skill"
    assert result["source"] == source_url

    # The install path must be under .reyn/skills/, not the original repo.
    install_path = Path(result["path"])
    assert ".reyn" in install_path.parts or ".reyn" in str(install_path), \
        f"installed path not under .reyn/: {install_path}"
    assert (install_path / "SKILL.md").exists() or (install_path.parent / "SKILL.md").exists() or \
           Path(result["path"]).exists(), \
        f"SKILL.md not found at installed path {result['path']}"

    # skills.yaml must have the entry with source field.
    config_path = tmp_path / ".reyn" / "config" / "skills.yaml"
    assert config_path.exists(), "skills.yaml was not written"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    entry = raw["skills"]["entries"]["source-skill"]
    assert entry["enabled"] is True
    assert entry["visibility"] == "menu"  # #2971: replaces auto_invoke: true
    assert entry["source"] == source_url
    assert ".reyn" in entry["path"] or str(tmp_path) in entry["path"]

    # skill_installed event must be emitted.
    kinds = [k for k, _ in events.emitted]
    assert "skill_installed" in kinds, f"skill_installed event not emitted; got {kinds}"


# ── Test 2: threat-scan block on fetched SKILL.md ────────────────────────────


@pytest.mark.asyncio
async def test_skill_install_source_threat_scan_blocks_and_removes_clone(tmp_path, monkeypatch):
    """Tier 2: when the fetched SKILL.md description triggers a blocking threat,
    the handler returns status='blocked', the clone is removed, and skills.yaml
    is NOT written. RED if the handler writes config or leaves the clone on disk."""
    from reyn.core.op_runtime.skill_install import handle

    repo = _make_git_skill_repo(tmp_path / "repos", "evil-skill", "EVIL_THREAT_MARKER")
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

    monkeypatch.setattr(
        "reyn.core.op_runtime.skill_install.scan_for_threats",
        _fake_scan,
    )
    monkeypatch.setattr(
        "reyn.core.op_runtime.skill_install.first_blocking_match",
        lambda matches, threshold="block": matches[0] if matches else None,
    )

    op = SkillInstallIROp(kind="skill_install", source=source_url)
    result = await handle(op=op, ctx=ctx)

    assert result["status"] == "blocked", f"expected blocked, got {result}"

    # skills.yaml must NOT have an entry for evil-skill.
    config_path = tmp_path / ".reyn" / "config" / "skills.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    assert (raw or {}).get("skills", {}).get("entries", {}).get("evil-skill") is None, \
        "skills.yaml was written despite a blocking threat match"

    # The clone must be removed after a block.
    clone_dir = tmp_path / ".reyn" / "skills" / "evil-skill"
    assert not clone_dir.exists(), \
        f"clone directory {clone_dir} was not removed after threat block"


# ── Test 3: require_http_get gate is exercised ────────────────────────────────


@pytest.mark.asyncio
async def test_skill_install_source_gates_http_get(tmp_path, monkeypatch):
    """Tier 2: the handler calls require_http_get for non-local (https/ssh) sources.
    When http.get is NOT approved and a non-file source URL is given, a PermissionError
    is raised before any clone. file:// refs skip the gate (local only, no HTTP).

    Uses monkeypatch on _shallow_clone so the test does not require network access.
    The gate fires before the clone call, so the clone stub is never reached on deny."""
    from reyn.core.op_runtime import skill_install as _si_mod
    from reyn.core.op_runtime.skill_install import handle

    # Monkeypatch _shallow_clone to succeed (would only be reached on allow; on deny
    # the gate raises before clone). This avoids real network calls for https sources.
    async def _fake_clone(git_url, dest, ctx):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "SKILL.md").write_text(
            "---\nname: gated-skill\ndescription: Gated\n---\nBody.\n",
            encoding="utf-8",
        )
        return None  # success

    monkeypatch.setattr(_si_mod, "_shallow_clone", _fake_clone)

    # Non-file source — https host will be gate-checked.
    source_url = "https://github.com/example/gated-skill"

    # Create ctx WITHOUT http_get approval (http_get_approved=False).
    ctx, _events = _make_ctx(tmp_path, http_get_approved=False)

    op = SkillInstallIROp(kind="skill_install", source=source_url)
    with pytest.raises(PermissionError):
        await handle(op=op, ctx=ctx)

    # No clone should exist (gate fired before clone).
    clone_dir = tmp_path / ".reyn" / "skills" / "gated-skill"
    assert not clone_dir.exists(), "clone was created despite missing http.get permission"


# ── Test 4: subdir convention (// separator) ──────────────────────────────────


@pytest.mark.asyncio
async def test_skill_install_source_subdir_convention(tmp_path):
    """Tier 2: a source URL with '//' subdir separator installs the skill from
    the specified subdirectory. RED if the handler uses the repo root or fails."""
    from reyn.core.op_runtime.skill_install import handle

    repo = _make_git_skill_repo_with_subdir(
        tmp_path / "repos",
        subdir="skills/my-skill",
        name="subdir-skill",
        description="Lives in a subdir",
    )
    # Subdir convention: repo_url//skills/my-skill
    source_url = repo.as_uri() + "//skills/my-skill"

    ctx, _events = _make_ctx(tmp_path)
    op = SkillInstallIROp(kind="skill_install", source=source_url)
    result = await handle(op=op, ctx=ctx)

    assert result["status"] == "installed", f"expected installed, got {result}"
    assert result["name"] == "subdir-skill"
    assert result["description"] == "Lives in a subdir"


# ── Test 5: config-generation smoke (recovery contract) ──────────────────────


@pytest.mark.asyncio
async def test_skill_install_source_records_config_generation(tmp_path):
    """Tier 2: a source install calls record_config_generation — the recovery
    contract (CLAUDE.md gate) applies to source installs just as to local ones.
    Smoke check: a config generation file is written under .reyn/config/generations/
    after the install (mirrors test_skill_install_pr_c truncate-falsify contract)."""
    from reyn.core.events.config_generations import ConfigGenerationStore
    from reyn.core.events.config_recovery import config_generations_dir
    from reyn.core.op_runtime.skill_install import handle

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    repo = _make_git_skill_repo(tmp_path / "repos", "recover-source-skill", "Recoverable source skill")
    source_url = repo.as_uri()

    ctx, _events = _make_ctx(tmp_path, state_log=state_log)
    op = SkillInstallIROp(kind="skill_install", source=source_url)
    result = await handle(op=op, ctx=ctx)

    assert result["status"] == "installed", f"install failed: {result}"

    # A config generation file must exist under .reyn/config/generations/ — this is
    # the truncation-surviving recovery base. The store writes rel@seq.yaml files.
    gen_dir = config_generations_dir(tmp_path / ".reyn")
    assert gen_dir.exists(), "config generations dir was not created — record_config_generation not called"
    gen_files = list(gen_dir.glob("*.yaml"))
    assert gen_files, \
        f"no generation files in {gen_dir} — record_config_generation did not write a snapshot"


# ── Test 6/7: SECURITY — path-traversal → arbitrary rmtree is refused ─────────


@pytest.mark.asyncio
async def test_skill_install_source_frontmatter_name_traversal_refused(tmp_path):
    """Tier 2: SECURITY — a malicious SKILL.md frontmatter ``name: ../../../evil``
    (third-party content) must NOT let the install escape .reyn/skills/. The
    install is refused (status='error') and a sentinel directory OUTSIDE
    .reyn/skills/ is neither created nor removed.

    RED against the pre-fix code (unsanitized frontmatter name flows into
    new_dest → shutil.rmtree of an arbitrary directory)."""
    from reyn.core.op_runtime.skill_install import handle

    # Sentinel dir OUTSIDE .reyn/skills/ — the traversal target would land here.
    sentinel = tmp_path / "victim"
    sentinel.mkdir(parents=True, exist_ok=True)
    (sentinel / "important.txt").write_text("do not delete me", encoding="utf-8")

    # A git repo whose SKILL.md frontmatter name traverses up to the sentinel.
    # ../../../../<tmp>/victim would, unsanitized, resolve to the sentinel dir.
    malicious_name = "../../../../victim"
    repo = _make_git_skill_repo(
        tmp_path / "repos", name=malicious_name, description="Traversal attack",
    )
    source_url = repo.as_uri()

    ctx, _events = _make_ctx(tmp_path)
    # Pass op.name that is safe so the candidate/clone step succeeds, then the
    # MALICIOUS frontmatter name is picked up at the rename step (the vuln point).
    op = SkillInstallIROp(kind="skill_install", source=source_url)
    result = await handle(op=op, ctx=ctx)

    # Frontmatter name wins over the URL basename in precedence; the malicious
    # name must be rejected → status='error', not 'installed'.
    assert result["status"] == "error", f"traversal name was not refused: {result}"
    assert "invalid skill name" in result.get("error", "").lower() or \
           "escapes" in result.get("error", "").lower(), \
        f"error did not indicate name rejection / containment: {result}"

    # The sentinel dir and its file MUST still exist (no arbitrary rmtree).
    assert sentinel.exists(), "SECURITY: sentinel directory was deleted (arbitrary rmtree)"
    assert (sentinel / "important.txt").exists(), \
        "SECURITY: sentinel file was deleted (path-traversal succeeded)"

    # No skills.yaml entry for the malicious name.
    config_path = tmp_path / ".reyn" / "config" / "skills.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    entries = (raw or {}).get("skills", {}).get("entries", {})
    assert not any(".." in k for k in entries), \
        f"SECURITY: a traversal-named entry was registered: {list(entries)}"


@pytest.mark.asyncio
async def test_skill_install_source_op_name_traversal_refused(tmp_path):
    """Tier 2: SECURITY — a caller-supplied ``op.name = '../../x'`` must NOT let
    the clone destination escape .reyn/skills/. The install is refused and a
    sentinel directory outside .reyn/skills/ is neither created nor removed.

    RED against the pre-fix code (unsanitized op.name flows into _candidate_name
    → clone_dest → shutil.rmtree in _shallow_clone)."""
    from reyn.core.op_runtime.skill_install import handle

    sentinel = tmp_path / "victim2"
    sentinel.mkdir(parents=True, exist_ok=True)
    (sentinel / "keep.txt").write_text("keep", encoding="utf-8")

    repo = _make_git_skill_repo(tmp_path / "repos", name="benign", description="Benign")
    source_url = repo.as_uri()

    ctx, _events = _make_ctx(tmp_path)
    op = SkillInstallIROp(
        kind="skill_install", source=source_url, name="../../../../victim2",
    )
    result = await handle(op=op, ctx=ctx)

    assert result["status"] == "error", f"op.name traversal was not refused: {result}"
    assert "invalid skill name" in result.get("error", "").lower() or \
           "escapes" in result.get("error", "").lower(), \
        f"error did not indicate name rejection / containment: {result}"

    # Sentinel must survive — no clone dir was created there and nothing removed.
    assert sentinel.exists(), "SECURITY: sentinel2 directory was deleted (arbitrary rmtree)"
    assert (sentinel / "keep.txt").exists(), \
        "SECURITY: sentinel2 file was deleted (path-traversal via op.name succeeded)"


# ── Test 9: SECURITY — clone routes through the sandbox abstraction (#2620) ──


@pytest.mark.asyncio
async def test_skill_install_clone_routes_through_sandbox_abstraction(tmp_path):
    """Tier 2: SECURITY (#2620) — _shallow_clone's git clone subprocess must go
    THROUGH the sandbox abstraction (``backend.run()``), not a raw
    ``subprocess.run`` that never consults any backend. Verified via a REAL
    ``NoopBackend`` subclass that records each ``run()`` invocation (not a
    mock) injected on ``ctx.sandbox_backend`` — RED if the clone bypasses the
    injected backend (e.g. reverts to a bare subprocess.run call)."""
    from reyn.core.op_runtime.skill_install import handle
    from reyn.security.sandbox.noop_backend import NoopBackend

    class _RecordingBackend(NoopBackend):
        """A real NoopBackend subclass that records each run() call's argv."""

        def __init__(self) -> None:
            super().__init__()
            self.calls: list[list[str]] = []

        async def run(self, argv, policy, **kwargs):
            self.calls.append(list(argv))
            return await super().run(argv, policy, **kwargs)

    repo = _make_git_skill_repo(tmp_path / "repos", "routed-skill", "Routed through sandbox")
    source_url = repo.as_uri()

    ctx, _events = _make_ctx(tmp_path)
    recording_backend = _RecordingBackend()
    ctx.sandbox_backend = recording_backend

    op = SkillInstallIROp(kind="skill_install", source=source_url)
    result = await handle(op=op, ctx=ctx)

    assert result["status"] == "installed", f"expected installed, got {result}"
    assert recording_backend.calls, (
        "git clone did not route through the injected sandbox backend's run() — "
        "the abstraction was bypassed"
    )
    clone_call = recording_backend.calls[0]
    assert clone_call[:2] == ["git", "clone"], f"unexpected clone argv: {clone_call}"
    assert source_url in clone_call


def test_safe_skill_name_rejects_traversal_and_separators():
    """Tier 2: SECURITY — _safe_skill_name rejects traversal / separator / hidden
    names and accepts plain slugs. Direct unit coverage of the sanitizer contract."""
    from reyn.core.op_runtime.skill_install import _safe_skill_name

    # Unsafe → None.
    for unsafe in [
        "", "   ", "..", ".", "../evil", "../../x", "a/b", "a\\b",
        ".hidden", "foo/../bar", "a b", "na\x00me", "évil",  # non-ascii homoglyph
    ]:
        assert _safe_skill_name(unsafe) is None, f"expected None for unsafe name {unsafe!r}"

    # Safe → unchanged.
    for safe in ["my-skill", "code_review", "skill.v2", "ABC123", "a-b_c.d"]:
        assert _safe_skill_name(safe) == safe, f"expected {safe!r} accepted unchanged"
