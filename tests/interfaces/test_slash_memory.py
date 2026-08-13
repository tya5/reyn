"""Tier 2: /memory slash command surfaces memory entries from chat.

Pins the new read-only command's surface contract: the registry
includes ``/memory``, the completer returns entry names after
``view ``, and the list / view subcommands route to handlers that
read through ``reyn.data.memory.list_entries`` / ``find_one``.
"""
from __future__ import annotations

import sys

import pytest

from tests._support.paths import REPO_ROOT

_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.slash import REGISTRY
from reyn.interfaces.slash.memory import _memory_completer


@pytest.mark.asyncio
async def test_memory_slash_is_registered():
    """Tier 2: ``/memory`` is in the slash registry, summary matches contract."""
    cmd = REGISTRY.get("memory")
    assert cmd is not None
    # Subcommand names landed in ``cmd.usage`` after PR #552 +
    # follow-on usage extension; summary is prose only now.
    assert "list" in cmd.usage.lower()
    assert "view" in cmd.usage.lower()
    assert cmd.completer is not None


@pytest.mark.asyncio
async def test_memory_completer_returns_names_after_view(tmp_path):
    """Tier 2: ``view <partial>`` completer returns memory entry names.

    Drives the completer through a real ``list_entries`` call against a tmp
    memory dir resolved via ``session.workspace_dir`` (#3721 — the completer
    resolves the project root from the real session it's handed, not ambient
    cwd) — no monkeypatching of internal collaborators (per testing.ja.md
    "Use real instances or the LLMReplay Fake").
    """
    mem_dir = tmp_path / ".reyn" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "user_role.md").write_text(
        '---\nname: user-role\ndescription: "Who you are"\n'
        'metadata:\n  type: user\n---\n\nBody.\n',
        encoding="utf-8",
    )
    (mem_dir / "tui_workflow.md").write_text(
        '---\nname: tui-workflow\ndescription: "How tui-coder works"\n'
        'metadata:\n  type: feedback\n---\n\nBody.\n',
        encoding="utf-8",
    )

    class _Session:
        agent_name = "test"
        workspace_dir = tmp_path / ".reyn" / "agents" / "test"

    result = _memory_completer(_Session(), "view ")
    assert set(result) == {"user-role", "tui-workflow"}


@pytest.mark.asyncio
async def test_memory_completer_returns_empty_for_list_subcommand():
    """Tier 2: ``list`` takes no name arg — completer returns []."""
    class _Session:
        agent_name = "test"

    assert _memory_completer(_Session(), "list") == []


@pytest.mark.asyncio
async def test_memory_completer_returns_empty_when_no_subcommand():
    """Tier 2: empty arg_partial → hint mode, no completions."""
    class _Session:
        agent_name = "test"

    assert _memory_completer(_Session(), "") == []
