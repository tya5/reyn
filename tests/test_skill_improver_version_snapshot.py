"""Tier 2: skill_improver version snapshot + on_propose gate (FP-0006 B+D).

Component B — version_snapshot.save_snapshot():
  Snapshot lifecycle: first-save, increment, max-versions cap (with and without
  current-pointer protection). All I/O uses real tmp_path — no mocks.

Component D — SelfImprovementConfig / load_config():
  Config parsing defaults, YAML override, invalid value rejection.

Testing policy: Tier 2 (OS invariant — deterministic logic + real fs ops).
No MagicMock / AsyncMock / patch. No private-state assertions.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

# ── helpers ────────────────────────────────────────────────────────────────────

def _make_skill_dir(tmp_path: Path, skill_name: str, skill_md_content: str = "") -> Path:
    """Create a minimal skill directory under tmp_path."""
    root = tmp_path / "reyn" / "local" / skill_name
    root.mkdir(parents=True, exist_ok=True)
    content = skill_md_content or (
        f"---\ntype: skill\nname: {skill_name}\nentry: go\n"
        "final_output: user_message\ngraph:\n  go: []\n---\n"
    )
    (root / "skill.md").write_text(content, encoding="utf-8")
    return root


def _make_artifact(skill_root: str, termination_reason: str = "score_threshold_met") -> dict:
    """Build a minimal improvement_result artifact dict for save_snapshot."""
    return {
        "data": {
            "termination_reason": termination_reason,
            "original_skill_root": skill_root,
            "files_modified": [],
        }
    }


def _make_versions_dir(tmp_path: Path, skill_name: str) -> Path:
    """Return the expected .reyn/skill-versions/<skill_name>/ path."""
    return tmp_path / ".reyn" / "skill-versions" / skill_name


# ── Component B tests ──────────────────────────────────────────────────────────


def test_first_save_creates_v1_and_current_pointer(tmp_path, monkeypatch):
    """Tier 2: first-ever save writes v1.md with original content and sets current=1."""
    monkeypatch.chdir(tmp_path)

    skill_name = "my_app"
    original_content = "# original skill.md content"
    skill_root = _make_skill_dir(tmp_path, skill_name, skill_md_content=original_content)

    from reyn.stdlib.skills.skill_improver.version_snapshot import save_snapshot

    artifact = _make_artifact(str(skill_root))
    result = save_snapshot(artifact)

    versions_dir = _make_versions_dir(tmp_path, skill_name)
    v1 = versions_dir / "v1.md"
    current_file = versions_dir / "current"

    # v1.md must exist with the original content
    assert v1.exists(), "v1.md should be created on first save"
    assert v1.read_text(encoding="utf-8") == original_content

    # current file must point to 1 (the pre-apply state just snapshotted)
    assert current_file.exists(), "current pointer file should exist"
    assert current_file.read_text(encoding="utf-8").strip() == "1"

    # return value
    assert result["saved_version"] == 1
    assert result["next_version"] == 2
    assert result["snapshot_path"].endswith("v1.md")
    assert skill_name in result["versions_dir"]


def test_subsequent_save_increments_version(tmp_path, monkeypatch):
    """Tier 2: with existing v1 + current=1, save_snapshot writes v2.md and updates current=2."""
    monkeypatch.chdir(tmp_path)

    skill_name = "my_app"
    original_content = "# v2 pre-apply content"
    skill_root = _make_skill_dir(tmp_path, skill_name, skill_md_content=original_content)

    versions_dir = _make_versions_dir(tmp_path, skill_name)
    versions_dir.mkdir(parents=True, exist_ok=True)

    # Pre-populate: v1.md already exists, current = "1"
    (versions_dir / "v1.md").write_text("# v1 original", encoding="utf-8")
    (versions_dir / "current").write_text("1", encoding="utf-8")

    from reyn.stdlib.skills.skill_improver.version_snapshot import save_snapshot

    artifact = _make_artifact(str(skill_root))
    result = save_snapshot(artifact)

    v2 = versions_dir / "v2.md"
    current_file = versions_dir / "current"

    assert v2.exists(), "v2.md should be created on second save"
    assert v2.read_text(encoding="utf-8") == original_content

    # current updates to 2 (the pre-apply snapshot version)
    assert current_file.read_text(encoding="utf-8").strip() == "2"

    assert result["saved_version"] == 2
    assert result["next_version"] == 3

    # v1.md must still exist (not deleted)
    assert (versions_dir / "v1.md").exists()


def test_max_versions_cap_drops_oldest(tmp_path, monkeypatch):
    """Tier 2: when versions exceed max_versions, the oldest vN.md is deleted."""
    monkeypatch.chdir(tmp_path)

    skill_name = "capped_skill"
    skill_root = _make_skill_dir(tmp_path, skill_name)

    versions_dir = _make_versions_dir(tmp_path, skill_name)
    versions_dir.mkdir(parents=True, exist_ok=True)

    # Pre-populate v1..v10, current = "10"
    for i in range(1, 11):
        (versions_dir / f"v{i}.md").write_text(f"# version {i}", encoding="utf-8")
    (versions_dir / "current").write_text("10", encoding="utf-8")

    from reyn.stdlib.skills.skill_improver import version_snapshot

    # Patch _get_max_versions to return 10 so cap is enforced at exactly 10
    monkeypatch.setattr(version_snapshot, "_get_max_versions", lambda: 10)

    artifact = _make_artifact(str(skill_root))
    result = save_snapshot_fn = version_snapshot.save_snapshot
    result = save_snapshot_fn(artifact)

    # v11 must be created
    assert (versions_dir / "v11.md").exists(), "v11.md should be written"

    # v1 must be deleted (oldest)
    assert not (versions_dir / "v1.md").exists(), "v1.md should be pruned (oldest)"

    # current is updated to 11 (the pre-apply snapshot)
    assert (versions_dir / "current").read_text(encoding="utf-8").strip() == "11"

    assert result["saved_version"] == 11


def test_max_versions_cap_never_deletes_current(tmp_path, monkeypatch):
    """Tier 2: the version pointed to by current (save_n) is never deleted during cap enforcement.

    Setup: v1..v10 exist, current = "10". save_snapshot reads current=10, so
    save_n = 11 → writes v11.md, sets current = "11". Cap enforcement at
    max_versions=10: sees 11 files, must drop 1. Protects save_n=11 (the
    just-written version). Drops v1 (oldest non-11). v2..v10 survive.
    """
    monkeypatch.chdir(tmp_path)

    skill_name = "protected_skill"
    skill_root = _make_skill_dir(tmp_path, skill_name)

    versions_dir = _make_versions_dir(tmp_path, skill_name)
    versions_dir.mkdir(parents=True, exist_ok=True)

    # Pre-populate v1..v10 with current = "10"
    for i in range(1, 11):
        (versions_dir / f"v{i}.md").write_text(f"# version {i}", encoding="utf-8")
    (versions_dir / "current").write_text("10", encoding="utf-8")

    from reyn.stdlib.skills.skill_improver import version_snapshot

    # max_versions = 10: after adding v11 (total=11) → drop 1 oldest non-current
    monkeypatch.setattr(version_snapshot, "_get_max_versions", lambda: 10)

    artifact = _make_artifact(str(skill_root))
    result = version_snapshot.save_snapshot(artifact)

    # v11 must be created (the new snapshot = save_n)
    assert (versions_dir / "v11.md").exists(), "v11.md should be written"

    # save_n = 11 is protected; v1 (oldest, not 11) must be dropped
    assert not (versions_dir / "v1.md").exists(), "v1.md should be pruned (oldest non-current)"

    # v2..v10 must survive (they are not the oldest after v1 is dropped)
    for i in range(2, 11):
        assert (versions_dir / f"v{i}.md").exists(), f"v{i}.md should not be deleted"

    # current updated to 11
    assert (versions_dir / "current").read_text(encoding="utf-8").strip() == "11"
    assert result["saved_version"] == 11


def test_noop_when_termination_not_score_threshold(tmp_path, monkeypatch):
    """Tier 2: save_snapshot returns noop dict when termination_reason != score_threshold_met."""
    monkeypatch.chdir(tmp_path)

    skill_name = "no_apply_skill"
    skill_root = _make_skill_dir(tmp_path, skill_name)

    from reyn.stdlib.skills.skill_improver.version_snapshot import save_snapshot

    artifact = _make_artifact(str(skill_root), termination_reason="max_iterations_reached")
    result = save_snapshot(artifact)

    assert result["saved_version"] is None
    assert result["next_version"] is None

    # No versions directory should be created
    versions_dir = _make_versions_dir(tmp_path, skill_name)
    assert not versions_dir.exists(), "versions dir should not be created for noop path"


def test_noop_when_stdlib_path(tmp_path, monkeypatch):
    """Tier 2: save_snapshot returns noop dict for src/ paths (stdlib guard)."""
    monkeypatch.chdir(tmp_path)

    from reyn.stdlib.skills.skill_improver.version_snapshot import save_snapshot

    artifact = _make_artifact("src/reyn/stdlib/skills/some_skill")
    result = save_snapshot(artifact)

    assert result["saved_version"] is None


# ── Component D tests ──────────────────────────────────────────────────────────


def test_self_improvement_config_defaults():
    """Tier 2: default ReynConfig carries on_propose=ask_user and max_versions=10."""
    from reyn.config import ReynConfig

    cfg = ReynConfig()
    assert cfg.self_improvement.on_propose == "ask_user"
    assert cfg.self_improvement.max_versions == 10


def test_self_improvement_config_yaml_override(tmp_path, monkeypatch):
    """Tier 2: reyn.yaml self_improvement block overrides both knobs."""
    reyn_yaml = tmp_path / "reyn.yaml"
    reyn_yaml.write_text(
        textwrap.dedent("""\
            self_improvement:
              on_propose: auto
              max_versions: 5
        """),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    from reyn.config import load_config
    cfg = load_config(cwd=tmp_path)

    assert cfg.self_improvement.on_propose == "auto"
    assert cfg.self_improvement.max_versions == 5


def test_self_improvement_config_rejects_invalid_on_propose():
    """Tier 2: SelfImprovementConfig raises ValueError for unknown on_propose values."""
    from reyn.config import SelfImprovementConfig

    with pytest.raises(ValueError, match="on_propose"):
        SelfImprovementConfig(on_propose="nonsense")


def test_on_propose_decision_logic():
    """Tier 2: pure decision function for on_propose gate covers all three branches."""
    # Test the pure function that maps (on_propose, score, threshold) → action
    # This exercises the domain logic without touching InterventionBus (Tier 3).
    from reyn.stdlib.skills.skill_improver.version_snapshot import decide_on_propose_action

    assert decide_on_propose_action("auto", 0.9, 0.85) == "auto_apply"
    assert decide_on_propose_action("ask_user", 0.9, 0.85) == "ask"
    assert decide_on_propose_action("disabled", 0.9, 0.85) == "dry_run"
    # Score below threshold — never reached in practice (apply_improvements guards this)
    # but the function should still return the configured action (gate is on_propose only)
    assert decide_on_propose_action("auto", 0.5, 0.85) == "auto_apply"
