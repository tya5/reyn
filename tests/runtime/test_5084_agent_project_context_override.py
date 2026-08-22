"""Tier 2: #5084 ② — an agent-layer ``project_context_path`` override gives
THAT agent its OWN REYN.md/AGENTS.md, resolved through the shared
``resolve_agent_project_context`` (:mod:`reyn.runtime.registry_bootstrap`) —
the same function both ``build_agent_registry_from_project`` (``reyn pipe``)
and ``reyn chat``'s own ``chat.py::_session_factory`` call, so this is not
a pipe-only witness.

This directly serves the owner's own stated goal for #5084: "2 coders
declared/provisioned just by writing profile.yaml, no slash commands" —
each agent's own file REPLACES the project-wide one for that agent's
session (never additive — the EXISTING ``.reyn/agents/<name>/AGENTS.md``
composition, ``RouterHostAdapter.get_project_context``, is a DIFFERENT,
untouched mechanism).

Real ``AgentRegistry``/``Session`` construction throughout — no mocks.
"""
from __future__ import annotations

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
async def test_agent_layer_project_context_path_replaces_the_project_wide_file(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: witness — two agents, each with its OWN
    ``project_context_path`` in profile.yaml, resolve to their OWN,
    DIFFERENT REYN.md content — neither sees the other's, and neither
    sees the project-wide ``AGENTS.md``/``REYN.md`` at all (a full
    replacement, not additive)."""
    project_root = tmp_path / "project"
    _write_reyn_yaml(project_root)
    (project_root / "AGENTS.md").write_text("project-wide instructions", encoding="utf-8")

    (project_root / "coder1-context.md").write_text("coder1's own context", encoding="utf-8")
    (project_root / "coder2-context.md").write_text("coder2's own context", encoding="utf-8")

    monkeypatch.chdir(project_root)
    from reyn.config import load_config

    config = load_config()
    registry = build_agent_registry_from_project(project_root, config, non_interactive=True)
    try:
        registry.create("coder1")
        registry.create("coder2")
        # Hand-edit each profile.yaml the way #5084's own target workflow
        # does -- no create_agent(project_context_path=...) parameter
        # exists (deliberately out of scope: the goal is a person writing
        # the file directly).
        p1 = AgentProfile.load(registry.agent_workspace_dir("coder1"))
        AgentProfile(
            name=p1.name, role=p1.role, created_at=p1.created_at,
            project_context_path="${REYN_PROJECT_DIR}/coder1-context.md",
        ).save(registry.agent_workspace_dir("coder1"))
        p2 = AgentProfile.load(registry.agent_workspace_dir("coder2"))
        AgentProfile(
            name=p2.name, role=p2.role, created_at=p2.created_at,
            project_context_path="${REYN_PROJECT_DIR}/coder2-context.md",
        ).save(registry.agent_workspace_dir("coder2"))

        session1 = registry.get_or_load("coder1")
        session2 = registry.get_or_load("coder2")

        assert session1.router_host.get_project_context() == "coder1's own context", (
            f"got {session1.router_host.get_project_context()!r}"
        )
        assert session2.router_host.get_project_context() == "coder2's own context", (
            f"got {session2.router_host.get_project_context()!r}"
        )
        assert "project-wide instructions" not in session1.router_host.get_project_context()
        assert "coder2's own context" not in session1.router_host.get_project_context()
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_default_agent_still_gets_the_project_wide_file(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: regression guard — an agent with NO project_context_path
    override (every pre-#5084 agent, including the default one) is
    completely unaffected: it still resolves the project-wide file
    exactly as before."""
    project_root = tmp_path / "project"
    _write_reyn_yaml(project_root)
    (project_root / "AGENTS.md").write_text("project-wide instructions", encoding="utf-8")

    monkeypatch.chdir(project_root)
    from reyn.config import load_config

    config = load_config()
    registry = build_agent_registry_from_project(project_root, config, non_interactive=True)
    try:
        session = registry.get_or_load(DEFAULT_AGENT_NAME)
        assert session.router_host.get_project_context() == "project-wide instructions"
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_project_context_path_outside_workspace_is_rejected(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: strip-falsifier for the ⊆workspace bound — a
    ``project_context_path`` pointing OUTSIDE the project workspace is
    never used; the session falls back to the project-wide file.

    Strip-falsifier: removing the ``within_workspace`` check in
    ``resolve_agent_project_context`` turns this red — verified locally."""
    project_root = tmp_path / "project"
    _write_reyn_yaml(project_root)
    (project_root / "AGENTS.md").write_text("project-wide instructions", encoding="utf-8")
    outside_file = tmp_path / "outside-workspace.md"
    outside_file.write_text("smuggled instructions", encoding="utf-8")

    monkeypatch.chdir(project_root)
    from reyn.config import load_config

    config = load_config()
    registry = build_agent_registry_from_project(project_root, config, non_interactive=True)
    try:
        registry.create("coder1")
        p1 = AgentProfile.load(registry.agent_workspace_dir("coder1"))
        AgentProfile(
            name=p1.name, role=p1.role, created_at=p1.created_at,
            project_context_path=str(outside_file),
        ).save(registry.agent_workspace_dir("coder1"))

        session = registry.get_or_load("coder1")
        assert session.router_host.get_project_context() == "project-wide instructions", (
            f"an out-of-workspace project_context_path must not be used; "
            f"got {session.router_host.get_project_context()!r}"
        )
    finally:
        await registry.shutdown()
