"""Tier 2: #5742 PR2 (architect ruling, issue #5742) — the agent-layer
``project_context_path`` override (#5084/#5111) is fully RETIRED: the
``AgentProfile`` field is gone, ``AgentProfile.load`` raises
``RetiredProfileKeyError`` naming the replacement (``context_path``), and
the WRITE side (``AgentRegistry.create``/``create_agent``'s own kwarg,
``reyn agent new --project-context-path``) is removed along with it — a
live creation seam that kept writing a key ``load()`` now hard-rejects
would silently make the created agent unstartable.

Replaces (deleted, not patched — the whole feature under test is gone):
``tests/runtime/test_5084_agent_project_context_override.py``,
``tests/cli/test_5111_agent_new_project_context_path.py``.

Real ``AgentRegistry``/argparse construction throughout — no mocks.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from reyn.runtime.profile import (
    AgentProfile,
    RetiredProfileKeyError,
    retired_profile_keys_present,
    unknown_profile_keys,
)
from reyn.runtime.registry import AgentRegistry
from tests._support.paths import REPO_ROOT

_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.cli.commands import agent as agent_cmd  # noqa: E402


def _no_factory(profile):
    raise RuntimeError("session factory not used in these write-side tests")


def _profile_path(project_root: Path, name: str) -> Path:
    return project_root / ".reyn" / "agents" / name / "profile.yaml"


# ── load()-time hard error ──────────────────────────────────────────────


def test_load_raises_retired_profile_key_error_naming_the_replacement(
    tmp_path: Path,
) -> None:
    """Tier 2: strip-falsifier target — a profile.yaml naming the retired
    ``project_context_path`` key raises at load, with the replacement
    (``context_path``) named in the message.

    Strip-falsifier: removing the retired-key check block from
    ``AgentProfile.load`` (leaving only the generic ``unknown_profile_
    keys`` WARN) turns this red — verified locally."""
    agent_dir = tmp_path / "coder1"
    agent_dir.mkdir()
    (agent_dir / "profile.yaml").write_text(
        "name: coder1\nrole: tester\ncreated_at: '2026-01-01'\n"
        "project_context_path: coder1-context.md\n",
        encoding="utf-8",
    )
    with pytest.raises(RetiredProfileKeyError, match="context_path"):
        AgentProfile.load(agent_dir)


def test_load_succeeds_without_the_retired_key(tmp_path: Path) -> None:
    """Tier 2: regression guard — a profile.yaml with no retired key
    (every current caller) loads normally; the hard error is scoped to
    the retired key's presence, not a blanket new failure mode."""
    agent_dir = tmp_path / "coder1"
    agent_dir.mkdir()
    (agent_dir / "profile.yaml").write_text(
        "name: coder1\nrole: tester\ncreated_at: '2026-01-01'\n"
        "context_path: REYN.md\n",
        encoding="utf-8",
    )
    profile = AgentProfile.load(agent_dir)
    assert profile.context_path == "REYN.md"
    assert not hasattr(profile, "project_context_path")


def test_retired_profile_keys_present_is_a_separate_population_from_unknown(
) -> None:
    """Tier 2: the two helper functions must not double-count — a retired
    key is reported by ``retired_profile_keys_present`` (which names the
    replacement) and specifically EXCLUDED from ``unknown_profile_keys``'s
    own generic bucket (its own docstring's claim: understating a retired
    key to a bare "no further signal" WARN would be a regression)."""
    data = {"name": "coder1", "project_context_path": "x.md", "bogus_key": 1}
    assert retired_profile_keys_present(data) == {
        "project_context_path": "context_path",
    }
    assert unknown_profile_keys(data) == frozenset({"bogus_key"})


# ── the write side is gone, not merely undocumented ─────────────────────


def test_registry_create_no_longer_accepts_project_context_path_kwarg(
    tmp_path: Path,
) -> None:
    """Tier 2: strip-falsifier — ``AgentRegistry.create`` raising
    ``TypeError`` for the retired kwarg (not silently accepting and
    discarding it) is the concrete proof the write seam is REMOVED, not
    merely undocumented. A caller passing it gets an immediate, loud
    failure at the call site — never a profile.yaml written with a key
    that then makes the agent unloadable."""
    reg = AgentRegistry(project_root=tmp_path, session_factory=_no_factory)
    with pytest.raises(TypeError):
        reg.create("coder1", project_context_path="x.md")  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_create_agent_no_longer_accepts_project_context_path_kwarg(
    tmp_path: Path,
) -> None:
    """Tier 2: the async creation seam (``create_agent``, what the CLI/
    web/slash surfaces actually call) rejects the retired kwarg the same
    way its sync ``create`` does."""
    reg = AgentRegistry(project_root=tmp_path, session_factory=_no_factory)
    with pytest.raises(TypeError):
        await reg.create_agent("coder1", project_context_path="x.md")  # type: ignore[call-arg]


def test_cli_no_longer_parses_the_project_context_path_flag() -> None:
    """Tier 2: strip-falsifier — ``reyn agent new --project-context-path``
    is no longer a recognized flag on the REAL top-level parser (the same
    one every CLI command module wires into), matching
    ``argparse``'s own "unrecognized arguments" ``SystemExit`` rather
    than a clean parse. This closes the gap #5111 opened (the flag WAS
    the sanctioned declaration surface for #5084's own goal); removing
    only the field/read-side and leaving this flag in place would let an
    operator write an unloadable profile.yaml through the CLI itself."""
    parser = argparse.ArgumentParser(prog="reyn")
    sub = parser.add_subparsers(dest="<command>", metavar="<command>")
    agent_cmd.register(sub)
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["agent", "new", "coder1", "--project-context-path", "x.md"],
        )


def test_cli_new_still_works_without_the_retired_flag(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: regression guard — ``reyn agent new`` itself (no retired
    flag involved) is unaffected; the removal is scoped to the one flag."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reyn.yaml").write_text("llm:\n  model: standard\n", encoding="utf-8")

    parser = argparse.ArgumentParser(prog="reyn")
    sub = parser.add_subparsers(dest="<command>", metavar="<command>")
    agent_cmd.register(sub)
    args = parser.parse_args(["agent", "new", "coder1"])
    args.func(args)

    profile_path = _profile_path(tmp_path, "coder1")
    assert profile_path.is_file()
    assert "project_context_path" not in profile_path.read_text(encoding="utf-8")


# ── end-to-end: a stale on-disk profile is unstartable, loudly ──────────


@pytest.mark.asyncio
async def test_an_existing_profile_with_the_retired_key_fails_to_load_via_the_registry(
    tmp_path: Path,
) -> None:
    """Tier 2: the property that actually matters to an operator — a
    profile.yaml migrated from BEFORE PR2 (or hand-restored from a
    ``.bak-5742`` backup) fails LOUDLY through the real
    ``AgentRegistry.get_or_load`` path, not just via a direct
    ``AgentProfile.load`` call in isolation. Mirrors how a real migration
    incident would surface: an operator's existing agent stops starting
    until they edit the file, with an error naming exactly what to do."""
    reg = AgentRegistry(project_root=tmp_path, session_factory=_no_factory)
    await reg.create_agent("coder1")
    profile_dir = reg.agent_workspace_dir("coder1")
    profile = AgentProfile.load(profile_dir)
    # Simulate a pre-PR2 profile.yaml (hand-written, or from a backup) —
    # dataclasses.replace can't set a field that no longer exists, so
    # write the retired key directly onto disk, the same shape a real
    # stale file would have.
    (profile_dir / "profile.yaml").write_text(
        f"name: {profile.name}\nrole: {profile.role!r}\n"
        f"created_at: {profile.created_at!r}\n"
        "project_context_path: coder1-context.md\n",
        encoding="utf-8",
    )
    with pytest.raises(RetiredProfileKeyError):
        reg.get_or_load("coder1")
