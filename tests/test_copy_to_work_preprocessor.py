"""Tier 2b: copy_to_work phase preprocessor invariants.

Guards the deterministic file-copy logic introduced to replace the LLM-driven
copy loop that caused B4-H2 / B5-M3 (workspace not created, eval cascade
FileNotFoundError). All work is done in the preprocessor; no LLM call needed.

Invariants tested:
  - Workspace directory is created (skill.md + phases/*.md written)
  - skill.md is faithfully copied to the target
  - All phase/*.md files (excluding eval.md) are copied
  - Files from sibling skills are never leaked into the work dir (B4-L1)

Test isolation: each test creates its own tmp_path + monkeypatch.chdir so
all default-zone file permission checks (CWD-relative) pass without
extra configuration.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.compiler.loader import load_dsl_skill
from reyn.data.workspace.workspace import Workspace
from reyn.events.events import EventLog
from reyn.kernel.preprocessor_executor import PreprocessorExecutor

# ── helpers ────────────────────────────────────────────────────────────────────


def _make_fake_skill(tmp_path: Path, skill_name: str) -> Path:
    """Create a minimal skill DSL tree under tmp_path/reyn/local/<skill_name>/."""
    root = tmp_path / "reyn" / "local" / skill_name
    (root / "phases").mkdir(parents=True)
    (root / "skill.md").write_text(
        "---\ntype: skill\nname: fake\nentry: go\nfinal_output: user_message\n"
        "graph:\n  go: []\n---\n",
        encoding="utf-8",
    )
    (root / "phases" / "go.md").write_text(
        "---\ntype: phase\nname: go\ninput: user_message\ncan_finish: true\n---\nDo the thing.\n",
        encoding="utf-8",
    )
    (root / "phases" / "analyze.md").write_text(
        "---\ntype: phase\nname: analyze\ninput: user_message\n---\nAnalyze.\n",
        encoding="utf-8",
    )
    # eval.md should be excluded from the copy
    (root / "phases" / "eval.md").write_text(
        "---\ntype: eval\nskill: fake\n---\n",
        encoding="utf-8",
    )
    return root


def _make_sibling_skill(tmp_path: Path, skill_name: str) -> Path:
    """Create a sibling skill that must NOT leak into the work dir."""
    root = tmp_path / "reyn" / "local" / skill_name
    (root / "phases").mkdir(parents=True)
    (root / "skill.md").write_text(
        "---\ntype: skill\nname: sibling\nentry: run\nfinal_output: user_message\n"
        "graph:\n  run: []\n---\n",
        encoding="utf-8",
    )
    (root / "phases" / "run.md").write_text(
        "---\ntype: phase\nname: run\ninput: user_message\ncan_finish: true\n---\nRun.\n",
        encoding="utf-8",
    )
    return root


def _make_artifact(skill_name: str) -> dict:
    """Build a minimal improvement_session artifact for copy_to_work input.

    After Wave 1 (B6-S1-H1 fix), the artifact carries only ``target_skill``
    (a short skill name).  All path fields are derived by the preprocessor via
    ``resolve_skill_path`` — they are NOT in the LLM-emitted artifact.
    """
    return {
        "type": "improvement_session",
        "data": {
            "target_skill": skill_name,
            "case_name": "basic",
            "case_input": "hello",
            "phase_criteria": [],
            "model": "standard",
            "max_iterations": 3,
            "score_threshold": 0.85,
            "improvement_focus": "",
        },
    }


def _run_preprocessor(tmp_path: Path, artifact: dict) -> dict:
    """Load the skill_improver skill and run the copy_to_work preprocessor."""
    # Load from the worktree's src tree so we pick up in-progress edits
    # rather than the installed package (which may lag behind worktree changes).
    skill_path = (
        Path(__file__).parent.parent
        / "src"
        / "reyn"
        / "stdlib"
        / "skills"
        / "skill_improver"
        / "skill.md"
    )
    skill = load_dsl_skill(skill_path)
    phase = skill.phases["copy_to_work"]

    events = EventLog()
    ws = Workspace(events=events)
    executor = PreprocessorExecutor(
        skill=skill,
        workspace=ws,
        model="standard",
        events=events,
        subscribers=[],
        resolver=None,  # no model resolver needed for pure-op preprocessors
        permission_resolver=None,  # default-zone writes (.reyn/) are allowed
    )
    result, _usage = asyncio.run(executor.run(phase, artifact, output_language=None))
    return result


# ── tests ──────────────────────────────────────────────────────────────────────


def test_copy_to_work_creates_workspace_dir(tmp_path, monkeypatch):
    """Tier 2b: preprocessor creates the .reyn/skill_improver_work/<slug>/ directory.

    After the preprocessor runs, the work dir must exist as a real directory
    so subsequent phases can write to it.
    """
    monkeypatch.chdir(tmp_path)
    _make_fake_skill(tmp_path, "my_skill")
    artifact = _make_artifact("my_skill")

    _run_preprocessor(tmp_path, artifact)

    work_dir = tmp_path / ".reyn" / "skill_improver_work" / "my_skill"
    assert work_dir.is_dir(), f"Expected work dir to exist: {work_dir}"


def test_copy_to_work_copies_skill_md(tmp_path, monkeypatch):
    """Tier 2b: skill.md is faithfully copied to the work directory.

    The work dir's skill.md must have identical content to the source.
    """
    monkeypatch.chdir(tmp_path)
    skill_root = _make_fake_skill(tmp_path, "my_skill")
    artifact = _make_artifact("my_skill")
    source_skill_md = skill_root / "skill.md"

    _run_preprocessor(tmp_path, artifact)

    work_dir = tmp_path / ".reyn" / "skill_improver_work" / "my_skill"
    copied_skill_md = work_dir / "skill.md"
    assert copied_skill_md.exists(), "skill.md was not copied to work dir"
    assert copied_skill_md.read_text(encoding="utf-8") == source_skill_md.read_text(
        encoding="utf-8"
    ), "skill.md content differs between source and work dir"


def test_copy_to_work_copies_all_phase_files(tmp_path, monkeypatch):
    """Tier 2b: all phases/*.md files (excluding eval.md) are copied to the work dir.

    go.md and analyze.md must be present. eval.md must be absent (it's excluded
    by the preprocessor to prevent the improver from modifying its own eval spec).
    """
    monkeypatch.chdir(tmp_path)
    _make_fake_skill(tmp_path, "my_skill")
    artifact = _make_artifact("my_skill")

    _run_preprocessor(tmp_path, artifact)

    work_dir = tmp_path / ".reyn" / "skill_improver_work" / "my_skill"
    phases_dir = work_dir / "phases"
    assert phases_dir.is_dir(), "phases/ subdirectory not created"

    assert (phases_dir / "go.md").exists(), "go.md not copied"
    assert (phases_dir / "analyze.md").exists(), "analyze.md not copied"
    # eval.md is explicitly excluded from the copy
    assert not (phases_dir / "eval.md").exists(), "eval.md should be excluded from the copy"


def test_copy_to_work_glob_does_not_leak_other_skills(tmp_path, monkeypatch):
    """Tier 2b: files from sibling skills are not copied into the work dir (B4-L1).

    When multiple skills exist under the same parent directory, the glob is
    scoped to original_skill_root only — no leakage into sibling skill directories.
    """
    monkeypatch.chdir(tmp_path)
    _make_fake_skill(tmp_path, "my_skill")
    _make_sibling_skill(tmp_path, "other_skill")
    artifact = _make_artifact("my_skill")

    _run_preprocessor(tmp_path, artifact)

    work_dir = tmp_path / ".reyn" / "skill_improver_work" / "my_skill"
    # No file from the sibling skill should appear anywhere under the work dir
    all_files = list(work_dir.rglob("*"))
    sibling_names = {"run.md"}  # files unique to sibling skill
    leaked = [f for f in all_files if f.name in sibling_names]
    assert not leaked, (
        f"Sibling skill files leaked into work dir: {[str(f) for f in leaked]}"
    )
    # Sibling work dir must not have been created either
    sibling_work_dir = tmp_path / ".reyn" / "skill_improver_work" / "other_skill"
    assert not sibling_work_dir.exists(), "Sibling skill work dir must not be created"
