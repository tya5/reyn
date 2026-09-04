"""Tier 2: #5742 — the agent frame (``profile.yaml``'s ``context_path``,
resolved through ``RouterHostAdapter._read_agent_instructions``) is HOT:
an edit to the resolved file, or to ``context_path`` itself, is reflected
the very next call — no session restart, no cache to invalidate — unlike
the project frame, which stays frozen at session construction (#3787
ruling, unchanged by #5742).

Real ``AgentRegistry``/``Session`` construction throughout — no mocks,
matching ``test_5084_agent_project_context_override.py``'s own shape for
this exact family (the sibling, still-supported ``project_context_path``
mechanism this file leaves untouched).
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml

from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import DEFAULT_AGENT_NAME
from reyn.runtime.registry_bootstrap import build_agent_registry_from_project


def _write_reyn_yaml(project_root: Path) -> None:
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "reyn.yaml").write_text(
        yaml.dump({"llm": {"model": "standard"}}, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_agent_frame_default_order_prefers_reyn_md(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: the #5742 default-order flip, exercised at the agent frame
    through a REAL ``RouterHostAdapter.get_project_context()`` call —
    when the default agent's own workspace has both ``REYN.md`` and
    ``AGENTS.md``, and ``context_path`` is unset, ``REYN.md`` wins."""
    project_root = tmp_path / "project"
    _write_reyn_yaml(project_root)

    monkeypatch.chdir(project_root)
    from reyn.config import load_config

    config = load_config()
    registry = build_agent_registry_from_project(project_root, config, non_interactive=True)
    try:
        session = registry.get_or_load(DEFAULT_AGENT_NAME)
        workspace_dir = registry.agent_workspace_dir(DEFAULT_AGENT_NAME)
        (workspace_dir / "REYN.md").write_text("agent reyn content", encoding="utf-8")
        (workspace_dir / "AGENTS.md").write_text("agent agents content", encoding="utf-8")

        assert session.router_host.get_project_context() == "agent reyn content"
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_agent_frame_context_path_is_hot_no_restart_needed(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the CENTRAL new-mechanism witness — editing ``profile.
    yaml``'s own ``context_path`` field on disk, mid-session, changes what
    the NEXT ``get_project_context()`` call returns, with no session
    reconstruction. This is the property the entire #5742 agent-frame
    design depends on ("agent 側は hot" — owner ruling); a cached/frozen
    read would return the OLD value here.

    Strip-falsifier: caching ``AgentProfile.load(...).context_path`` at
    ``RouterHostAdapter`` construction time (instead of re-reading it
    inside ``_read_agent_instructions`` on every call) turns this red —
    verified locally."""
    project_root = tmp_path / "project"
    _write_reyn_yaml(project_root)
    # Deliberately NO project-wide REYN.md/AGENTS.md here -- isolates this
    # witness to the agent frame alone (the composition contract is its
    # own separate test below).

    monkeypatch.chdir(project_root)
    from reyn.config import load_config

    config = load_config()
    registry = build_agent_registry_from_project(project_root, config, non_interactive=True)
    try:
        registry.create("coder1")
        workspace_dir = registry.agent_workspace_dir("coder1")
        (workspace_dir / "first.md").write_text("first version", encoding="utf-8")
        (workspace_dir / "second.md").write_text("second version", encoding="utf-8")

        profile = AgentProfile.load(workspace_dir)
        dataclasses.replace(profile, context_path="first.md").save(workspace_dir)

        session = registry.get_or_load("coder1")
        assert session.router_host.get_project_context() == "first version"

        # Edit context_path on disk, mid-session — no restart.
        profile2 = AgentProfile.load(workspace_dir)
        dataclasses.replace(profile2, context_path="second.md").save(workspace_dir)

        assert session.router_host.get_project_context() == "second version", (
            "the agent frame must re-read profile.yaml's context_path on "
            "every call (LIVE reload class) -- a stale value here means "
            "the read got cached/frozen somewhere"
        )
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_agent_frame_and_project_frame_compose_additively(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: regression guard for ``get_project_context()``'s own
    additive-composition contract (#3787, unchanged by #5742) — when BOTH
    the project-wide file and the agent's own ``context_path``-resolved
    file have content, both appear, each under its own sub-heading."""
    project_root = tmp_path / "project"
    _write_reyn_yaml(project_root)
    (project_root / "AGENTS.md").write_text("project-wide instructions", encoding="utf-8")

    monkeypatch.chdir(project_root)
    from reyn.config import load_config

    config = load_config()
    registry = build_agent_registry_from_project(project_root, config, non_interactive=True)
    try:
        session = registry.get_or_load(DEFAULT_AGENT_NAME)
        workspace_dir = registry.agent_workspace_dir(DEFAULT_AGENT_NAME)
        (workspace_dir / "REYN.md").write_text("agent's own text", encoding="utf-8")

        rendered = session.router_host.get_project_context()
        assert "project-wide instructions" in rendered
        assert "agent's own text" in rendered
    finally:
        await registry.shutdown()
