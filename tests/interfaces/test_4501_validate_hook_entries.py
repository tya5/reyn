"""Tier 2: #4501 / #4364 PR-1 — ``reyn config validate`` widened to open
each ``hooks:`` list entry (the free-form nested structure the top-level
schema walk in #4235 never recurses into) via the real ``load_hooks``
parser, catching a malformed/wrong-scope per-hook key — not just an
unrecognized TOP-LEVEL config key.

Companion to ``test_4235_validate_in_set.py`` (that PR's own top-level
IN-set coverage, unchanged by this one — its accept/reject tests still
pass verbatim). The concrete motivating case (architect's real 3-hour
incident): ``allow_write_paths`` (the agent-level ``sandbox.policy``
field name) written inside a ``hooks:`` entry instead of the per-hook
key (``write_paths``) — ``validate`` passed ("No unknown ... keys
found") while the hook silently did nothing.

#4501 covered exactly ONE of hooks' three real input paths (the
``.reyn/config/hooks.yaml`` runtime IN-set). #4364 PR-1 found the other
two the same night, mirroring ``Session._build_hook_registry``'s own
3-layer COMBINE: reyn.yaml's own top-level ``hooks:`` (the layer
``docs/concepts/runtime/hooks.md`` actually tells operators to write in —
and where architect's real incident lived, NOT the IN-set #4501 fixed)
and every ``.reyn/agents/<name>/hooks.yaml``. The tests below cover those
two additions; the IN-set tests above are unchanged.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr("reyn.config._find_project_root", lambda _cwd: tmp_path)
    monkeypatch.setattr("reyn.config.loader._find_project_root", lambda _cwd: tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_allow_write_paths_inside_a_hook_entry_is_now_caught(project, capsys):
    """Tier 2: the exact motivating incident — a hook entry using the
    agent-level sandbox.policy field name instead of the per-hook key is
    now reported as a labeled finding, not silently accepted."""
    _write_yaml(project / "reyn.yaml", MINIMAL_REYN_YAML)
    _write_yaml(
        project / ".reyn" / "config" / "hooks.yaml",
        "hooks:\n"
        "  - \"on\": turn_end\n"
        "    exec: [\"echo\", \"hi\"]\n"
        "    allow_write_paths: [\"/tmp\"]\n",
    )
    from reyn.interfaces.cli.commands.config import _validate

    _validate()
    out = capsys.readouterr().out
    assert "Hook entry validation" in out
    assert "allow_write_paths" in out
    assert "write_paths" in out
    # It must not be misreported as an applied-but-inert or top-level
    # unknown-key finding — those sections describe a different defect
    # class and carry a different (wrong) remedy.
    assert "Policy tier" not in out


def test_an_unrelated_unknown_hook_key_is_also_caught(project, capsys):
    """Tier 2: any unrecognized per-hook key is caught, not just the one
    named wrong-scope hint — a plain typo gets the generic message."""
    _write_yaml(project / "reyn.yaml", MINIMAL_REYN_YAML)
    _write_yaml(
        project / ".reyn" / "config" / "hooks.yaml",
        "hooks:\n"
        "  - \"on\": turn_end\n"
        "    exec: [\"echo\", \"hi\"]\n"
        "    nam: typo\n",
    )
    from reyn.interfaces.cli.commands.config import _validate

    _validate()
    out = capsys.readouterr().out
    assert "Hook entry validation" in out
    assert "nam" in out


def test_a_well_formed_hooks_list_produces_no_hook_entry_finding(project, capsys):
    """Tier 2: accept-side — a real, valid hooks: list must not trip the
    new section — a false positive here teaches operators to ignore the
    report, the same #4174 T0 concern #4235's own accept test already
    named for the top-level case."""
    _write_yaml(project / "reyn.yaml", MINIMAL_REYN_YAML)
    _write_yaml(
        project / ".reyn" / "config" / "hooks.yaml",
        "hooks:\n"
        "  - \"on\": turn_end\n"
        "    exec: [\"echo\", \"hi\"]\n"
        "    write_paths: [\"/tmp\"]\n",
    )
    from reyn.interfaces.cli.commands.config import _validate

    _validate()
    out = capsys.readouterr().out
    assert "Hook entry validation" not in out
    assert "No unknown, renamed, or disabled-by-dependency config keys found." in out


def test_an_absent_hooks_key_is_not_treated_as_a_hook_entry_error(project, capsys):
    """Tier 2: accept-side — a project with no hooks: configured at all
    (the common case) must not spuriously grow the new section."""
    _write_yaml(project / "reyn.yaml", MINIMAL_REYN_YAML)
    _write_yaml(
        project / ".reyn" / "config" / "mcp.yaml",
        "mcp:\n  servers: {}\n",
    )
    from reyn.interfaces.cli.commands.config import _validate

    _validate()
    out = capsys.readouterr().out
    assert "Hook entry validation" not in out


# ── #4364 PR-1: reyn.yaml top-level hooks: (the startup layer) ─────────────


def test_a_malformed_entry_in_reyn_yamls_own_hooks_block_is_now_caught(project, capsys):
    """Tier 2: #4364 PR-1 ② — the layer #4501 did NOT cover. This is the
    layer docs/concepts/runtime/hooks.md tells operators to write in, and
    the one architect's own real incident actually lived in (they had no
    .reyn/config/hooks.yaml at all that night — every hook was in
    reyn.yaml's own top-level hooks: block)."""
    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML
        + "hooks:\n"
        "  - \"on\": turn_end\n"
        "    exec: [\"echo\", \"hi\"]\n"
        "    allow_write_paths: [\"/tmp\"]\n",
    )
    from reyn.interfaces.cli.commands.config import _validate

    _validate()
    out = capsys.readouterr().out
    assert "Hook entry validation" in out
    assert "[reyn.yaml]" in out
    assert "allow_write_paths" in out
    assert "write_paths" in out


def test_a_well_formed_reyn_yaml_hooks_block_produces_no_finding(project, capsys):
    """Tier 2: accept-side for the reyn.yaml source — a valid top-level
    hooks: block must not trip the new section."""
    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML
        + "hooks:\n"
        "  - \"on\": turn_end\n"
        "    exec: [\"echo\", \"hi\"]\n"
        "    write_paths: [\"/tmp\"]\n",
    )
    from reyn.interfaces.cli.commands.config import _validate

    _validate()
    out = capsys.readouterr().out
    assert "Hook entry validation" not in out
    assert "No unknown, renamed, or disabled-by-dependency config keys found." in out


# ── #4364 PR-1: .reyn/agents/<name>/hooks.yaml (the per-agent layer) ───────


def test_a_malformed_entry_in_a_per_agent_hooks_file_is_now_caught(project, capsys):
    """Tier 2: #4364 PR-1 ③ — the third real input path, previously
    invisible to validate entirely (no prior test covered it, IN-set or
    otherwise). The finding is labeled with the specific agent+path so an
    operator with multiple agents knows exactly which file to fix."""
    _write_yaml(project / "reyn.yaml", MINIMAL_REYN_YAML)
    _write_yaml(
        project / ".reyn" / "agents" / "planner" / "hooks.yaml",
        "hooks:\n"
        "  - \"on\": turn_end\n"
        "    exec: [\"echo\", \"hi\"]\n"
        "    allow_write_paths: [\"/tmp\"]\n",
    )
    from reyn.interfaces.cli.commands.config import _validate

    _validate()
    out = capsys.readouterr().out
    assert "Hook entry validation" in out
    assert ".reyn/agents/planner/hooks.yaml" in out
    assert "allow_write_paths" in out
    assert "write_paths" in out


def test_a_well_formed_per_agent_hooks_file_produces_no_finding(project, capsys):
    """Tier 2: accept-side for the per-agent source — a valid
    .reyn/agents/<name>/hooks.yaml must not trip the new section."""
    _write_yaml(project / "reyn.yaml", MINIMAL_REYN_YAML)
    _write_yaml(
        project / ".reyn" / "agents" / "planner" / "hooks.yaml",
        "hooks:\n"
        "  - \"on\": turn_end\n"
        "    exec: [\"echo\", \"hi\"]\n"
        "    write_paths: [\"/tmp\"]\n",
    )
    from reyn.interfaces.cli.commands.config import _validate

    _validate()
    out = capsys.readouterr().out
    assert "Hook entry validation" not in out
    assert "No unknown, renamed, or disabled-by-dependency config keys found." in out


def test_an_agent_dir_with_no_hooks_file_is_silently_skipped(project, capsys):
    """Tier 2: accept-side — an agent directory that exists (e.g. it has
    state/ from a prior run) but no hooks.yaml at all must not be treated
    as a malformed source; load_per_agent_hooks's own [] default covers
    this, this test pins that validate's new loop respects it."""
    _write_yaml(project / "reyn.yaml", MINIMAL_REYN_YAML)
    (project / ".reyn" / "agents" / "planner" / "state").mkdir(parents=True)
    from reyn.interfaces.cli.commands.config import _validate

    _validate()
    out = capsys.readouterr().out
    assert "Hook entry validation" not in out


def test_multiple_hook_sources_each_report_their_own_labeled_finding(project, capsys):
    """Tier 2: a malformed entry in BOTH reyn.yaml and a per-agent file at
    once produces two distinct labeled findings, not one that shadows the
    other — an operator fixing only the first-listed one must still see
    the second remains."""
    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML
        + "hooks:\n"
        "  - \"on\": turn_end\n"
        "    exec: [\"echo\", \"hi\"]\n"
        "    allow_write_paths: [\"/tmp\"]\n",
    )
    _write_yaml(
        project / ".reyn" / "agents" / "planner" / "hooks.yaml",
        "hooks:\n"
        "  - \"on\": turn_end\n"
        "    exec: [\"echo\", \"hi\"]\n"
        "    nam: typo\n",
    )
    from reyn.interfaces.cli.commands.config import _validate

    _validate()
    out = capsys.readouterr().out
    assert "[reyn.yaml]" in out
    assert ".reyn/agents/planner/hooks.yaml" in out
