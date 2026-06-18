"""Tier 2: /skills slash command — list available skills.

Regression test for PR #82. The previous implementation walked
``stdlib_root()`` looking for ``<name>/skill.md``, but the stdlib layout
is ``stdlib_root()/skills/<name>/skill.md``. The directory listing of
``stdlib_root()`` only contains ``artifacts/`` and ``skills/``, neither
of which has a ``skill.md`` at the top level, so the stdlib branch of
the output was always empty.

Asserting that the stdlib branch is non-empty when stdlib skills ship
in the source tree is the test that would have caught PR #82's bug.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session


def _make_session(tmp_path: Path, *, agent_name: str = "alpha") -> Session:
    """Build a Session redirected to ``tmp_path``."""
    return Session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


def _drain_outbox(session: Session) -> list:
    out = []
    while not session.outbox.empty():
        out.append(session.outbox.get_nowait())
    return out


@pytest.mark.asyncio
async def test_skills_lists_bundled_stdlib(tmp_path, monkeypatch):
    """Tier 2: /skills includes the bundled stdlib catalogue.

    The stdlib ships at least one skill (eval, direct_llm, …) under
    ``src/reyn/stdlib/skills/<name>/skill.md``. If the slash command's
    path resolution is correct, the output MUST mention the stdlib
    label and at least one bundled skill name.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    consumed = await session._maybe_handle_slash("/skills")
    assert consumed is True

    msgs = _drain_outbox(session)
    system_texts = [m.text for m in msgs if m.kind == "system"]
    combined = "\n".join(system_texts)

    # The bug PR #82 fixed: stdlib branch was always empty. Asserting
    # that a known-bundled stdlib skill appears under the stdlib label
    # is the minimal regression guard.
    assert "stdlib:" in combined, (
        f"/skills output is missing the stdlib label: {combined!r}"
    )
    # `eval` is one of the bundled stdlib skills shipped in this repo
    # (src/reyn/stdlib/skills/eval/skill.md). Any reasonable assertion
    # over the bundled catalogue picks one stable name.
    assert "eval" in combined, (
        f"/skills did not list any bundled stdlib skill: {combined!r}"
    )


@pytest.mark.asyncio
async def test_skills_lists_project_skills(tmp_path, monkeypatch):
    """Tier 2: /skills includes project-layer skills under ``reyn/project/``.

    Mirrors :func:`reyn.skill.skill_paths.resolve_skill_path` — the
    slash command MUST find skills written to the same project layout
    that the runtime resolver expects.
    """
    project_skill = tmp_path / "reyn" / "project" / "demo_skill"
    project_skill.mkdir(parents=True)
    (project_skill / "skill.md").write_text("# demo_skill\n")

    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    consumed = await session._maybe_handle_slash("/skills")
    assert consumed is True

    msgs = _drain_outbox(session)
    system_texts = [m.text for m in msgs if m.kind == "system"]
    combined = "\n".join(system_texts)

    assert "project:" in combined, (
        f"/skills output is missing the project label: {combined!r}"
    )
    assert "demo_skill" in combined, (
        f"/skills did not list the project-layer skill: {combined!r}"
    )
