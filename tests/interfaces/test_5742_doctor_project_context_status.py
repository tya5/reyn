"""Tier 2: #5742 — ``reyn doctor``'s new project-context section
(``_print_project_context_status``), driven through the real ``run()``
entry point against real on-disk fixtures — matching ``test_4364_pr3a_
doctor_cli.py``'s own established shape for this command family.

The central property under test is DERIVATION, not restatement (architect's
own acceptance item: "既定名順を変えたとき、doctor を触らずに doctor の出
力が変わる — 導出の witness") — proven below by monkeypatching the shared
``DEFAULT_PROJECT_CONTEXT_FILES`` constant and observing doctor's own
printed output change with zero edits to ``doctor.py`` itself.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from reyn.interfaces.cli.commands.doctor import run
from reyn.runtime.profile import AgentProfile
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML)
    return tmp_path


def _make_agent(project: Path, name: str, *, context_path: "str | None" = None) -> Path:
    agent_dir = project / ".reyn" / "agents" / name
    agent_dir.mkdir(parents=True)
    profile = AgentProfile.new(name, role="tester")
    if context_path is not None:
        import dataclasses

        profile = dataclasses.replace(profile, context_path=context_path)
    profile.save(agent_dir)
    return agent_dir


def test_project_frame_reports_the_resolved_path_not_the_config_value(
    project: Path, capsys,
) -> None:
    """Tier 2: owner's own question ① — "どのファイルが実際に読まれている
    か" — an UNSET ``project_context_path`` (nothing in ``reyn.yaml``)
    with a real ``REYN.md`` present must print the RESOLVED path, not
    merely "unset"."""
    (project / "REYN.md").write_text("hello", encoding="utf-8")
    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out
    assert "project:" in out
    assert str(project / "REYN.md") in out


def test_project_frame_distinguishes_no_candidate_from_unreadable(
    project: Path, capsys,
) -> None:
    """Tier 2: owner's own question ② — "未設定と『読めない』は別の答え。
    捏造しないこと" — an explicit pin naming a missing file prints a
    visibly DIFFERENT line from the no-file-at-all case, never folded
    into the same silent "nothing configured" wording."""
    run(Namespace(project_root=str(project)))
    out_unset = capsys.readouterr().out
    assert "not configured, no default-order file present" in out_unset

    _write_yaml(
        project / "reyn.yaml",
        MINIMAL_REYN_YAML + 'project_context_path: "NOPE.md"\n',
    )
    run(Namespace(project_root=str(project)))
    out_unreadable = capsys.readouterr().out
    assert "unreadable" in out_unreadable
    assert "not configured, no default-order file present" not in out_unreadable


def test_agent_frame_reports_per_agent_resolved_path(project: Path, capsys) -> None:
    """Tier 2: doctor's own agent-frame loop — one line per real
    ``.reyn/agents/<name>/`` directory, resolved via the SAME
    ``resolve_context_text`` the runtime side calls (not a second,
    hand-reconstructed check)."""
    agent_dir = _make_agent(project, "coder1")
    (agent_dir / "AGENTS.md").write_text("agent-1 instructions", encoding="utf-8")

    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out
    assert "agent [coder1]:" in out
    assert str(agent_dir / "AGENTS.md") in out


def test_no_agents_directory_prints_a_plain_disclosure_not_a_crash(
    project: Path, capsys,
) -> None:
    """Tier 2: a project with no ``.reyn/agents/`` at all (never bootstrapped)
    must not raise — doctor's own D-1 "report the real state" posture."""
    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out
    assert "no .reyn/agents/<name>/ found" in out


def test_default_order_flip_changes_doctor_output_with_zero_doctor_py_edits(
    project: Path, capsys, monkeypatch,
) -> None:
    """Tier 2: the derivation witness itself — flip
    :data:`~reyn.config.loader.DEFAULT_PROJECT_CONTEXT_FILES` (patched at
    its OWN definition site, ``reyn.config.loader``, the single place
    both frames' resolvers read it from) and observe doctor's own printed
    resolved-path line change to match, having touched nothing in
    ``doctor.py``. This is the concrete falsifier for "doctor derives its
    answer, it doesn't restate a copy" — if doctor had its own hardcoded
    ``("REYN.md", "AGENTS.md")`` tuple anywhere, this test would still be
    green with a stale answer; it isn't, because there's only one tuple
    for both to read."""
    import reyn.config.loader as loader_mod

    (project / "REYN.md").write_text("reyn content", encoding="utf-8")
    (project / "AGENTS.md").write_text("agents content", encoding="utf-8")

    run(Namespace(project_root=str(project)))
    out_before = capsys.readouterr().out
    assert str(project / "REYN.md") in out_before

    monkeypatch.setattr(
        loader_mod, "DEFAULT_PROJECT_CONTEXT_FILES", ("AGENTS.md", "REYN.md"),
    )
    run(Namespace(project_root=str(project)))
    out_after = capsys.readouterr().out
    assert str(project / "AGENTS.md") in out_after
    assert str(project / "REYN.md") not in out_after
