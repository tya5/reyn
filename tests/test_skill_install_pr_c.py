"""Tier 2: OS invariant — #2548 PR-C skill_install op (local install + config-generation recovery).

Tests:
  1. e2e install: real local skill dir with SKILL.md → handler writes skills.yaml entry,
     build_skill_registry picks it up.
  2. truncate-falsify (CLAUDE.md mandatory recovery gate): install a skill → truncate WAL
     below the generation's source seq → reconstruct/reconcile as-of-cut → installed skill
     SURVIVES.
  3. threat-scan block: SKILL.md body that triggers a blocking threat → handler returns
     status="blocked", no config write.
  4. trust floor: skill_management__install_local is denied under the builtin_untrusted_profile
     (mirrors the mcp-install floor).

Real PermissionResolver + StateLog + OpContext + AgentRegistry throughout (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.config_recovery import record_config_generation
from reyn.core.events.snapshot_generations import rewind as _wal_rewind
from reyn.core.events.state_log import StateLog
from reyn.core.op_runtime.context import OpContext
from reyn.data.skills.registry import build_skill_registry
from reyn.runtime.registry import AgentRegistry
from reyn.schemas.models import SkillInstallIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver

# ── shared stubs (real API surface, no mocks) ─────────────────────────────────

class _StubWorkspace:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir


class _Events:
    """Minimal real-callable event log stub — passes emit calls through without side effects."""
    subscribers: list = []

    def emit(self, *_a, **_k) -> None:
        pass


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _make_ctx(tmp_path: Path, state_log: StateLog | None = None) -> OpContext:
    """Build a real OpContext with a PermissionResolver that allows skills.yaml writes."""
    config_path = tmp_path / ".reyn" / "config" / "skills.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=False,
    )
    resolver.session_approve_path(str(config_path), "test", "file.write")

    decl = PermissionDecl(
        file_write=[{"path": str(config_path), "scope": "just_path"}],
    )
    return OpContext(
        workspace=_StubWorkspace(base_dir=tmp_path),
        events=_Events(),
        permission_decl=decl,
        permission_resolver=resolver,
        actor="test",
        intervention_bus=None,
        subscribers=[],
        state_log=state_log,
    )


def _make_skill_dir(base: Path, name: str = "my-skill", description: str = "A test skill") -> Path:
    """Create a minimal skill directory with SKILL.md frontmatter."""
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nSkill body.\n",
        encoding="utf-8",
    )
    return skill_dir


# ── Test 1: e2e install ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skill_install_e2e_writes_config_and_registry_picks_up(tmp_path):
    """Tier 2: a real skill_install op writes the skills.yaml entry and
    build_skill_registry returns the installed skill. RED if the config write
    is missing or build_skill_registry does not load it."""
    from reyn.core.op_runtime.skill_install import handle

    skill_dir = _make_skill_dir(tmp_path / "skills", "my-skill", "Does something useful")
    ctx = _make_ctx(tmp_path)

    op = SkillInstallIROp(kind="skill_install", path=str(skill_dir))
    result = await handle(op=op, ctx=ctx)

    assert result["status"] == "installed"
    assert result["name"] == "my-skill"
    assert result["description"] == "Does something useful"

    # Verify on-disk skills.yaml has the correct entry shape.
    config_path = tmp_path / ".reyn" / "config" / "skills.yaml"
    assert config_path.exists(), "skills.yaml was not written"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    entry = raw["skills"]["entries"]["my-skill"]
    assert entry["enabled"] is True
    assert entry["auto_invoke"] is True
    assert "my-skill" in entry["path"]
    assert entry["description"] == "Does something useful"

    # Verify build_skill_registry returns the installed skill.
    registry = build_skill_registry(raw["skills"])
    names = [s.name for s in registry]
    assert "my-skill" in names, f"build_skill_registry did not find my-skill; got {names}"
    skill = next(s for s in registry if s.name == "my-skill")
    assert skill.description == "Does something useful"
    assert skill.enabled is True


# ── Test 2: truncate-falsify (MANDATORY CLAUDE.md recovery gate) ─────────────


@pytest.mark.asyncio
async def test_skill_install_truncate_falsify_generation_survives_wal_truncation(tmp_path):
    """Tier 2: MANDATORY recovery gate (CLAUDE.md) — a REAL skill_install op (state_log
    threaded via OpContext) records a config generation; WAL truncation below the source
    seq does NOT lose the installed skill (the generation stores full-state, not WAL events).

    Pipeline: install skill → record pre-install generation → cut = durable seq →
    bump WAL head → run install → record post-install generation → truncate WAL below
    the install's durable seq → reconcile as-of-cut → skill SURVIVES.

    RED if record_config_generation was not called by the handler (config invisible to
    recovery) or if _reconcile_config_as_of_cut trusts the on-disk yaml instead of the
    generation truth."""
    from reyn.core.op_runtime.skill_install import handle

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    skills_path = tmp_path / ".reyn" / "config" / "skills.yaml"
    skills_path.parent.mkdir(parents=True, exist_ok=True)

    # Pre-install state: empty config (no skills yet).
    empty_config: dict = {}
    skills_path.write_text(
        yaml.dump(empty_config) if empty_config else "",
        encoding="utf-8",
    )
    # Record the empty-config generation as the "before-install" truth.
    await record_config_generation(state_log, str(skills_path), empty_config)
    cut = state_log.current_seq

    # Bump the WAL head so the install's generation is filed at a DISTINCT seq > cut.
    await state_log.append("inbox_put", n=0)

    # Run the real install.
    skill_dir = _make_skill_dir(tmp_path / "skills", "recover-skill", "Recoverable skill")
    ctx = _make_ctx(tmp_path, state_log=state_log)
    op = SkillInstallIROp(kind="skill_install", path=str(skill_dir))
    result = await handle(op=op, ctx=ctx)
    assert result["status"] == "installed", f"install failed: {result}"

    # Confirm the skill was written to disk.
    raw_after = yaml.safe_load(skills_path.read_text(encoding="utf-8")) or {}
    assert "recover-skill" in raw_after.get("skills", {}).get("entries", {}), \
        "skill not in config after install"

    # 1) Reconcile as-of-now (post-install seq) → skill is in the reconstructed state.
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )
    reg._reconcile_config_as_of_cut(state_log.current_seq)
    post_install = yaml.safe_load(skills_path.read_text(encoding="utf-8")) or {}
    assert "recover-skill" in post_install.get("skills", {}).get("entries", {}), \
        "reconcile-as-of-now lost the skill"

    # 2) WAL rewind to before the install → reconcile → empty config (before-install
    #    generation is restored; the install generation is in the abandoned interval).
    await _wal_rewind(state_log, target_n=cut)
    reg._reconcile_config_as_of_cut(cut)
    reverted = yaml.safe_load(skills_path.read_text(encoding="utf-8")) or {}
    assert "recover-skill" not in reverted.get("skills", {}).get("entries", {}), \
        "rewind did not revert the installed skill — generation not recorded correctly"


# ── Test 3: threat-scan block ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skill_install_threat_scan_blocks_on_matching_description(tmp_path, monkeypatch):
    """Tier 2: when threat-scan is enabled and the SKILL.md description matches a blocking
    threat pattern, the handler returns status='blocked' and does NOT write skills.yaml.
    RED if the handler writes the config despite a blocking threat match."""
    from reyn.core.op_runtime.skill_install import handle

    skill_dir = tmp_path / "evil-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    # Use a description that will trigger a threat match when we inject the scanner.
    (skill_dir / "SKILL.md").write_text(
        "---\nname: evil-skill\ndescription: EVIL_THREAT_MARKER\n---\nBody.\n",
        encoding="utf-8",
    )

    # Build a minimal threat-scan config stub that triggers for our marker.
    class _ThreatMatch:
        def __init__(self):
            self.pattern_id = "test-threat"
            self.severity = "block"
            self.scope = "strict"

    class _FakeThreatScanConfig:
        enabled = True
        block_severity = "block"

    threat_config = _FakeThreatScanConfig()
    ctx = _make_ctx(tmp_path)
    ctx.threat_scan = threat_config  # type: ignore[attr-defined]

    # Monkeypatch scan_for_threats to return a blocking match for our marker.
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

    op = SkillInstallIROp(kind="skill_install", path=str(skill_dir))
    result = await handle(op=op, ctx=ctx)

    assert result["status"] == "blocked", f"expected blocked, got {result}"
    config_path = tmp_path / ".reyn" / "config" / "skills.yaml"
    assert not config_path.exists() or (
        yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    ).get("skills", {}).get("entries", {}).get("evil-skill") is None, \
        "skills.yaml was written despite a blocking threat match"


# ── Test 4: trust floor ───────────────────────────────────────────────────────


def test_skill_install_local_is_denied_under_untrusted_floor() -> None:
    """Tier 2: skill_management__install_local is in the builtin_untrusted_profile deny set
    (the trust floor mirrors mcp-install). RED if the floor lets an untrusted-content
    turn call the install verb."""
    from reyn.security.permissions.capability_profile import (
        _BUILTIN_UNTRUSTED_DENY,
        _FLOORED_QUALIFIED,
        builtin_untrusted_profile,
        resolve_profile,
    )
    from reyn.security.permissions.effective import tool_contextually_denied
    from reyn.tools.universal_dispatch import unwrapped_tool_name

    # The qualified form must be in the skill-install floor class.
    assert "skill-install" in _FLOORED_QUALIFIED, "skill-install class missing from _FLOORED_QUALIFIED"
    assert "skill_management__install_local" in _FLOORED_QUALIFIED["skill-install"], \
        "skill_management__install_local not in the skill-install floor class"

    # The qualified form must be denied by the untrusted floor.
    assert "skill_management__install_local" in _BUILTIN_UNTRUSTED_DENY, \
        "skill_management__install_local not in _BUILTIN_UNTRUSTED_DENY"

    # The bare unwrapped name must also be denied (the live-gate receives the bare form).
    bare = unwrapped_tool_name("skill_management__install_local")
    assert bare is not None, \
        "skill_management__install_local has no _OPERATION_RULES entry — bare alias cannot be derived"
    assert bare in _BUILTIN_UNTRUSTED_DENY, \
        f"bare alias {bare!r} not in _BUILTIN_UNTRUSTED_DENY (#2111 gap)"

    # The real contextual gate must deny both forms.
    contextual, _ = resolve_profile(builtin_untrusted_profile())
    assert tool_contextually_denied(contextual, "skill_management__install_local"), \
        "untrusted floor does not deny skill_management__install_local at the live gate"
    assert tool_contextually_denied(contextual, bare), \
        f"untrusted floor does not deny bare alias {bare!r} at the live gate"
