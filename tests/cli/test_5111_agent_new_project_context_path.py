"""Tier 2: #5111 (lead-coder assignment, mirrors #5080's own ``--base-dir``
precedent exactly) — ``reyn agent new`` gains a ``--project-context-path``
flag reaching the SAME creation seam (``AgentRegistry.create_agent`` ->
``create``) ``--base-dir`` already writes through.

#5084's own owner-authored goal witness names the CREATION SEAM as the
ONLY declaration surface ("2 coders declared/provisioned just by writing
profile.yaml, no slash commands") — before this PR, ``base_dir`` was
declarable that way and ``project_context_path`` was not (#5111's own
finding, filed during #5084's live goal-witness verification). This
closes that gap at CREATION time only — ``agent edit`` is explicitly out
of scope (lead-coder's own instruction).

The READ side (``registry_bootstrap.resolve_agent_project_context``, the
``⊆workspace`` protect-at-use enforcement) is UNCHANGED and already
covered by ``test_5084_agent_project_context_override.py`` — this file
covers only the NEW write side: the CLI flag itself, and ``registry.
create``'s own eager ⊆workspace rejection (mirrors #5080's own witness ④
for ``base_dir``, same shape, same bound function).

Real ``AgentRegistry``/argparse parser construction throughout — no mocks.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests._support.paths import REPO_ROOT

_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.cli.commands import agent as agent_cmd  # noqa: E402
from reyn.runtime.registry import AgentRegistry  # noqa: E402


def _no_factory(profile):
    raise RuntimeError("session factory not used in these write-side tests")


def _profile_path(project_root: Path, name: str) -> Path:
    return project_root / ".reyn" / "agents" / name / "profile.yaml"


def _parse_agent_new(argv: "list[str]"):
    """Build the REAL top-level argparse parser (the same ``register``
    every CLI command module wires into) and parse ``argv`` — proves the
    flag is reachable from an actual command line, not just that
    ``_cmd_new`` accepts the kwarg when handed a hand-built Namespace."""
    import argparse

    parser = argparse.ArgumentParser(prog="reyn")
    sub = parser.add_subparsers(dest="<command>", metavar="<command>")
    agent_cmd.register(sub)
    return parser.parse_args(argv)


# ── witness ① — the CLI flag itself is wired ────────────────────────────────


def test_new_project_context_path_flag_is_parsed() -> None:
    """Tier 2: ``reyn agent new x --project-context-path ...`` parses to a
    real ``args.project_context_path`` attribute the SAME way
    ``--base-dir`` already does — the flag genuinely exists on the
    command line, not just as a ``create_agent`` keyword nobody can
    reach.

    Strip-falsifier: removing ``p_new.add_argument("--project-context-
    path", ...)`` turns this red with an argparse ``SystemExit`` (unknown
    argument) rather than a clean parse — verified locally."""
    args = _parse_agent_new(
        ["agent", "new", "coder1", "--project-context-path", "coder1-context.md"],
    )
    assert args.project_context_path == "coder1-context.md"


def test_new_without_the_flag_defaults_to_none() -> None:
    """Tier 2: regression guard — omitting the flag (every pre-#5111
    caller) still parses, with ``project_context_path=None`` (byte-
    identical to the pre-#5111 signature's implicit default)."""
    args = _parse_agent_new(["agent", "new", "coder1"])
    assert args.project_context_path is None


# ── witness ② — _cmd_new writes the key through the real creation seam ──────


def test_cmd_new_writes_project_context_path_to_profile(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: driving the REAL ``_cmd_new`` (the function ``reyn agent
    new`` actually dispatches to) with a parsed ``args.project_context_
    path`` writes a ``project_context_path:`` key into the created
    agent's own ``profile.yaml`` — the full CLI-to-disk path, not just
    the registry method in isolation (already covered elsewhere;
    ``test_5084_agent_project_context_override.py``'s own witness ①
    covers the create_agent->session resolution end)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reyn.yaml").write_text("llm:\n  model: standard\n", encoding="utf-8")

    args = _parse_agent_new(
        ["agent", "new", "coder1", "--project-context-path", "coder1-context.md"],
    )
    args.func(args)

    profile_path = _profile_path(tmp_path, "coder1")
    assert profile_path.is_file()
    text = profile_path.read_text(encoding="utf-8")
    assert "project_context_path:" in text, (
        f"no project_context_path: key was written to coder1's own "
        f"profile.yaml; got: {text!r}"
    )


# ── witness ③ — the write-side ⊆workspace bound (mirrors #5080's own ④) ────


@pytest.mark.asyncio
async def test_project_context_path_outside_the_workspace_is_rejected_at_write(
    tmp_path: Path,
) -> None:
    """Tier 2: the WRITE-side upper bound — a requested
    ``project_context_path`` outside the project workspace is REJECTED
    (a raised ``ValueError`` naming the boundary) at CREATE time, never
    silently clamped or accepted then quietly ignored later — the SAME
    shape ``base_dir`` already has (``test_5080_...::test_base_dir_
    outside_the_workspace_is_rejected``), via the SAME
    ``within_workspace`` bound function (#5084's own explicit
    instruction: reuse, never a second copy).

    This is the WRITE-side twin of ``test_5084_agent_project_context_
    override.py``'s own ``test_project_context_path_outside_workspace_
    is_rejected`` (the READ-side/protect-at-use test, for a value that
    reached profile.yaml some OTHER way, e.g. a direct hand-edit) — that
    test predates #5111 and is unaffected; this one is new, for the
    seam #5111 added.

    Strip-falsifier: removing the bound-check block this PR added to
    ``registry.create`` (leaving the ``project_context_path`` write
    unconditional) turns this green (accepted) instead of red —
    verified locally."""
    project_root = tmp_path / "project"
    outside_file = tmp_path / "outside-the-workspace.md"

    reg = AgentRegistry(project_root=project_root, session_factory=_no_factory)
    with pytest.raises(ValueError, match="outside the project workspace"):
        await reg.create_agent("coder1", project_context_path=str(outside_file))

    # Rejected atomically: no partial state survives a rejected create.
    assert not reg.exists("coder1")
    assert not _profile_path(project_root, "coder1").exists()


# ── witness ④ — omitting the flag writes no key (byte-identical) ───────────


@pytest.mark.asyncio
async def test_create_agent_without_project_context_path_writes_no_key(
    tmp_path: Path,
) -> None:
    """Tier 2: regression guard, registry level — omitting
    ``project_context_path`` (every pre-#5111 caller) writes NO
    ``project_context_path:`` key at all, mirroring #5080's own
    ``test_agent_created_without_base_dir_writes_no_override_key``."""
    reg = AgentRegistry(project_root=tmp_path, session_factory=_no_factory)
    await reg.create_agent("coder1")

    profile_path = _profile_path(tmp_path, "coder1")
    assert profile_path.is_file()
    assert "project_context_path" not in profile_path.read_text(encoding="utf-8")
