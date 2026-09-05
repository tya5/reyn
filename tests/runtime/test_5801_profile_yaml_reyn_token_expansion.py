"""Tier 2: #5801 — profile.yaml's ``context_path``/``base_dir`` are
reyn-token-aware fields (both name project-relative locations) but
``AgentProfile.load`` read them via a totally separate path from every
other reyn-yaml face (the config cascade / hooks.yaml), with NO token
expansion call anywhere. Real incident: an operator's
``context_path: ${REYN_PROJECT_DIR}/AGENTS.md`` never expanded --
``resolve_context_candidate`` looked for a literal ``${REYN_PROJECT_DIR}``
subdirectory under the agent's own workspace_dir, found none, and the
agent silently never read its own instructed text, every session, with
zero warning (there was nothing there to warn -- no expansion call
existed to notice the token was unresolved).

Witness discipline (lead-coder's own standing correction on this exact
family, #5801): "witness は「warning が出ない」ではなく「中身が system
prompt に入る」で" -- a passing test that only checks "no warning fired"
would also pass if ``context_path`` silently resolved to nothing and the
agent still never read anything. Every test below asserts the REAL file
CONTENT actually lands where the agent frame reads it
(``RouterHostAdapter.get_project_context()``, the same live surface
#5742's own hot-reload tests use) -- not merely the absence of a warning.

Real ``AgentRegistry``/``Session`` construction throughout -- no mocks.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry_bootstrap import build_agent_registry_from_project


def _write_reyn_yaml(project_root: Path) -> None:
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "reyn.yaml").write_text(
        yaml.dump({"llm": {"model": "standard"}}, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_context_path_reyn_project_dir_token_expands_and_content_is_read(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the exact real incident -- ``context_path:
    ${REYN_PROJECT_DIR}/CODER_BROWN_INSTRUCTIONS.md`` in profile.yaml,
    naming a file OUTSIDE the agent's own workspace_dir (at the project
    root, shared across agents -- the real coder-brown/coder-smith
    shape). Deliberately NOT named REYN.md/AGENTS.md (#5742's own
    project-frame default-order search would find those independently
    of context_path at all -- self-caught test-validity bug: an earlier
    draft of this test used AGENTS.md and stayed green even with the
    #5801 fix stripped out, because the project frame's OWN default
    read supplied the content regardless of whether context_path ever
    expanded). Real-content witness: ``get_project_context()`` returns
    the genuine file text, not merely "no warning fired"."""
    project_root = tmp_path / "project"
    _write_reyn_yaml(project_root)
    (project_root / "CODER_BROWN_INSTRUCTIONS.md").write_text(
        "# real project instructions\nbe careful with prod.\n", encoding="utf-8",
    )

    monkeypatch.chdir(project_root)
    from reyn.config import load_config

    config = load_config()
    registry = build_agent_registry_from_project(project_root, config, non_interactive=True)
    try:
        registry.create("coder-brown")
        profile_path = registry.agent_workspace_dir("coder-brown") / "profile.yaml"
        profile_path.write_text(
            "name: coder-brown\n"
            "role: brown\n"
            "created_at: '2026-01-01T00:00:00+00:00'\n"
            "context_path: ${REYN_PROJECT_DIR}/CODER_BROWN_INSTRUCTIONS.md\n",
            encoding="utf-8",
        )

        session = registry.get_or_load("coder-brown")
        composed = session.router_host.get_project_context()
        assert "# real project instructions\nbe careful with prod." in composed, (
            "the agent's own instructed text must actually be read, not "
            f"silently skipped -- got {composed!r}"
        )
    finally:
        await registry.shutdown()


def test_base_dir_reyn_project_dir_token_also_expands(tmp_path: Path) -> None:
    """Tier 2: the OTHER field lead-coder's review explicitly flagged --
    fixing context_path alone while leaving base_dir unexpanded is a
    known failure shape ("片方だけ直すと、もう片方が黙って残ります"). Real
    content witness: the resolved AgentProfile.base_dir is a genuine
    absolute path under project_root, not a literal unexpanded token."""
    project_root = tmp_path / "project"
    agent_dir = project_root / ".reyn" / "agents" / "coder-brown"
    agent_dir.mkdir(parents=True)
    (agent_dir / "profile.yaml").write_text(
        "name: coder-brown\n"
        "role: brown\n"
        "created_at: '2026-01-01T00:00:00+00:00'\n"
        "base_dir: ${REYN_PROJECT_DIR}/repos/coder-brown\n",
        encoding="utf-8",
    )

    profile = AgentProfile.load(agent_dir)

    assert profile.base_dir == str((project_root / "repos" / "coder-brown").resolve()) or (
        profile.base_dir == str(project_root / "repos" / "coder-brown")
    ), f"base_dir must be a real expanded path, not a literal token -- got {profile.base_dir!r}"
    assert "${REYN_PROJECT_DIR}" not in (profile.base_dir or "")


def test_context_path_unresolved_reyn_token_fails_closed(tmp_path: Path) -> None:
    """Tier 2: #5801 req② -- an unresolved reyn token in profile.yaml is
    reyn's own bug (it could not supply a value it owns), not an
    operator config choice to silently honor as a literal path. Uses
    ``${REYN_SKILL_DIR}`` -- a real member of reyn's own token vocabulary
    that profile.yaml's own map does not carry (only
    REYN_PROJECT_DIR/REYN_AGENT_NAME are this face's map, #5801 req③) --
    so this is a genuine "reyn cannot resolve this here" case, not a
    typo'd unrelated ${VAR}."""
    project_root = tmp_path / "project"
    agent_dir = project_root / ".reyn" / "agents" / "coder-brown"
    agent_dir.mkdir(parents=True)
    (agent_dir / "profile.yaml").write_text(
        "name: coder-brown\n"
        "role: brown\n"
        "created_at: '2026-01-01T00:00:00+00:00'\n"
        "context_path: ${REYN_SKILL_DIR}/AGENTS.md\n",
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match=r"left reyn token\(s\).*unresolved -- refusing"):
        profile = AgentProfile.load(agent_dir)

    # Real-content witness: refusal degrades the WHOLE file's contribution
    # (context_path falls back to its own None default), not a half-
    # applied partial expansion that would leave a literal token behind.
    assert profile.context_path is None, (
        f"a refused profile.yaml must not leave a literal unresolved token "
        f"in context_path -- got {profile.context_path!r}"
    )


def test_context_path_with_no_reyn_token_is_unaffected(tmp_path: Path) -> None:
    """Tier 2: accept-side control -- the common case, a bare filename
    with no reyn token at all, is completely unaffected by this change
    (#5742's own default shape, still the majority of real profile.yaml
    files)."""
    project_root = tmp_path / "project"
    agent_dir = project_root / ".reyn" / "agents" / "coder-brown"
    agent_dir.mkdir(parents=True)
    (agent_dir / "profile.yaml").write_text(
        "name: coder-brown\n"
        "role: brown\n"
        "created_at: '2026-01-01T00:00:00+00:00'\n"
        "context_path: REYN.md\n",
        encoding="utf-8",
    )

    profile = AgentProfile.load(agent_dir)

    assert profile.context_path == "REYN.md"
